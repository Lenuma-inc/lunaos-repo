import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


SCRIPT = Path(__file__).with_name('sync-archlinux.sh')
FAKE_LFTP = r'''#!/usr/bin/env python3
import json, os, re, shutil, sys
from pathlib import Path
commands = sys.stdin.read()
root = Path(os.environ['SYNC_TEST_ROOT'])
repo = re.search(r'archlinux/(core|multilib|extra)/|archlinux-mirror/(core|multilib|extra)/', commands)
repo = next(value for value in repo.groups() if value)
stage = 'publish' if 'mirror --reverse' in commands else 'packages' if 'cls -1' in commands else 'metadata'
with (root / 'calls.jsonl').open('a') as output:
    output.write(json.dumps({'repo': repo, 'stage': stage, 'commands': commands}) + '\n')
if os.environ.get('SYNC_TEST_FAIL') == stage:
    sys.exit(7)
if stage == 'metadata':
    target = re.search(r'"https://[^\"]+" "([^\"]+)"', commands).group(1)
    for source in (root / 'fixtures' / repo).iterdir():
        shutil.copyfile(source, Path(target) / source.name)
if stage == 'packages':
    target = re.search(r'cls -1 > "([^\"]+)"', commands).group(1)
    names = ['pkg-1-1-any.pkg.tar.zst', 'pkg-1-1-any.pkg.tar.zst.sig',
             'old-1-1-any.pkg.tar.zst', 'old-1-1-any.pkg.tar.zst.sig', repo + '.db', repo + '.db.sig', 'README', 'bad"pkg.tar.zst']
    if os.environ.get('SYNC_TEST_MISSING'):
        names.remove('pkg-1-1-any.pkg.tar.zst.sig')
    Path(target).write_text('\n'.join(names) + '\n')
'''


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        binary = self.root / 'bin'
        binary.mkdir()
        (binary / 'lftp').write_text(FAKE_LFTP)
        (binary / 'lftp').chmod(0o755)
        self.env = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'],
                        SYNC_TEST_ROOT=str(self.root), FTP_USERNAME='test-user',
                        FTP_PASSWORD='test-password', BUCKET_NAME='test-bucket')
        for repo in ('core', 'multilib', 'extra'):
            directory = self.root / 'fixtures' / repo
            directory.mkdir(parents=True)
            for suffix in ('db', 'files'):
                self.database(directory / (repo + '.' + suffix))

    def database(self, path, filename='pkg-1-1-any.pkg.tar.zst'):
        content = ('%FILENAME%\n' + filename + '\n').encode()
        with tarfile.open(path, 'w:gz') as archive:
            entry = tarfile.TarInfo('pkg-1-1/desc')
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))

    def run_sync(self, **env):
        return subprocess.run(['bash', str(SCRIPT)], env=dict(self.env, **env),
                              text=True, capture_output=True)

    def calls(self):
        path = self.root / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_success_publishes_databases_after_packages_then_removes_only_obsolete_packages(self):
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([call['stage'] for call in calls], ['metadata', 'packages', 'publish'] * 3)
        for call in calls:
            if call['stage'] == 'publish':
                commands = call['commands']
                self.assertIn('--transfer-all', commands)
                self.assertLess(commands.index('mirror --reverse'), commands.index('rm "old-'))
                self.assertNotIn('rm "pkg-', commands)
                self.assertNotIn('rm "README"', commands)
                self.assertNotIn('bad"pkg.tar.zst', commands)
                self.assertNotIn('rm "core.db"', commands)
                self.assertIn('rm "' + call['repo'] + '.db.sig"', commands)

    def test_transfer_failures_are_not_reported_as_success(self):
        for stage in ('metadata', 'packages', 'publish'):
            with self.subTest(stage=stage):
                (self.root / 'calls.jsonl').unlink(missing_ok=True)
                result = self.run_sync(SYNC_TEST_FAIL=stage)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('synchronization completed', result.stdout)
                self.assertEqual(self.calls()[-1]['stage'], stage)

    def test_missing_signature_blocks_database_publication_and_deletion(self):
        result = self.run_sync(SYNC_TEST_MISSING='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call['stage'] for call in self.calls()], ['metadata', 'packages'])

    def test_invalid_or_inconsistent_snapshots_block_package_transfer(self):
        for filename in ('../../escape.pkg.tar.zst', 'other-1-1-any.pkg.tar.zst'):
            with self.subTest(filename=filename):
                (self.root / 'calls.jsonl').unlink(missing_ok=True)
                self.database(self.root / 'fixtures/core/core.files', filename)
                result = self.run_sync()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([call['stage'] for call in self.calls()], ['metadata'])

    def test_corrupt_database_blocks_package_transfer(self):
        (self.root / 'fixtures/core/core.db').write_text('broken archive')
        result = self.run_sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call['stage'] for call in self.calls()], ['metadata'])

    def test_missing_credentials_and_unsafe_bucket_fail_before_network(self):
        for name in ('FTP_USERNAME', 'FTP_PASSWORD', 'BUCKET_NAME'):
            with self.subTest(name=name):
                self.assertNotEqual(self.run_sync(**{name: ''}).returncode, 0)
        self.assertNotEqual(self.run_sync(BUCKET_NAME='../bad;bucket').returncode, 0)
        self.assertEqual(self.calls(), [])

    @unittest.skipUnless(shutil.which('lftp'), 'lftp is not installed')
    def test_native_lftp_filters_packages_and_refreshes_existing_databases(self):
        source = self.root / 'source'
        target = self.root / 'target'
        source.mkdir()
        target.mkdir()
        for name in ('core.db', 'core.files', 'pkg-1-1-any.pkg.tar.zst',
                     'pkg-1-1-any.pkg.tar.zst.sig', 'unrelated.txt'):
            (source / name).write_text('new')
        required = self.root / 'required'
        required.write_text('pkg-1-1-any.pkg.tar.zst\npkg-1-1-any.pkg.tar.zst.sig\n')
        commands = f'''set cmd:fail-exit yes
set xfer:use-temp-file yes
mirror --no-recursion --dereference --no-perms --exclude-glob '*' --include '(^|/)core\\.(db|files)(\\.sig)?$' file://{source} {target}
mirror --no-recursion --dereference --no-perms --ignore-time --parallel=5 --exclude-glob '*' --include-glob-from={required} file://{source} file://{target}
bye
'''
        self.native_lftp(commands)
        self.assertEqual({p.name for p in target.iterdir()},
                         {'core.db', 'core.files', 'pkg-1-1-any.pkg.tar.zst', 'pkg-1-1-any.pkg.tar.zst.sig'})
        (target / 'pkg-1-1-any.pkg.tar.zst').write_text('partial')
        self.native_lftp(commands)
        self.assertEqual((target / 'pkg-1-1-any.pkg.tar.zst').read_text(), 'new')
        (source / 'core.db').write_text('refreshed database')
        self.native_lftp(f'set cmd:fail-exit yes\nmirror --reverse --no-recursion --no-perms --transfer-all "{source}" "file://{target}"\nbye\n')
        self.assertEqual((target / 'core.db').read_text(), 'refreshed database')

    def native_lftp(self, commands):
        result = subprocess.run([shutil.which('lftp')], input=commands, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
