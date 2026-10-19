import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


SCRIPT = Path(__file__).with_name('sync-archlinux.sh')
FAKE_LFTP = r'''#!/usr/bin/env python3
import json, os, re, shutil, sys
from pathlib import Path
root = Path(os.environ['SYNC_TEST_ROOT'])
commands = sys.stdin.read()
repo = re.search(r'archlinux/(core|multilib|extra)/', commands).group(1)
with (root / 'calls.jsonl').open('a') as out:
    out.write(json.dumps({'repo': repo, 'stage': 'metadata', 'commands': commands}) + '\n')
if os.environ.get('SYNC_TEST_FAIL') == 'metadata':
    sys.exit(7)
target = re.search(r'"https://[^\"]+" "([^\"]+)"', commands).group(1)
for source in (root / 'fixtures' / repo).iterdir():
    shutil.copyfile(source, Path(target) / source.name)
'''
FAKE_AWS = r'''#!/usr/bin/env python3
import fcntl, json, os, re, sys
from pathlib import Path
root = Path(os.environ['SYNC_TEST_ROOT'])
args = sys.argv[1:]
assert args[:4] == ['--endpoint-url', 'https://s3.ru1.storage.beget.cloud', '--region', 'ru1']
assert '--no-paginate' not in args and '--no-sign-request' not in args
config = Path(os.environ['AWS_CONFIG_FILE']).read_text()
assert 'addressing_style = path' in config and 'max_concurrent_requests = 1' in config
args = args[4:]
if args[:2] == ['s3', 'cp'] and args[3] == 's3://test-bucket/archlinux-mirror/lastsync':
    with (root / 'calls.jsonl').open('a') as out:
        out.write(json.dumps({'repo': None, 'stage': 'lastsync', 'args': args}) + '\n')
    if os.environ.get('SYNC_TEST_FAIL') == 'lastsync':
        sys.exit(7)
    (root / 'lastsync').write_bytes(Path(args[2]).read_bytes())
    sys.exit(0)
repo = re.search(r'archlinux-mirror/(core|multilib|extra)/', ' '.join(args)).group(1)
package = os.environ.get('SYNC_TEST_PACKAGE', 'pkg-1-1-any.pkg.tar.zst')
with (root / 'aws.lock').open('w') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    statefile = root / (repo + '.json')
    state = json.loads(statefile.read_text()) if statefile.exists() else {
        'lists': 0, 'sizes': {package: int(os.environ.get('SYNC_TEST_REMOTE_SIZE', '3')),
        package + '.sig': 9, 'old-1-1-any.pkg.tar.zst': 3,
        'old-1-1-any.pkg.tar.zst.sig': 9, repo + '.db': 1, repo + '.db.sig': 1,
        'README': 1, 'nested/pkg-1-1-any.pkg.tar.zst': 1, 'bad"pkg.tar.zst': 1}}
    sizes = state['sizes']
    if args[:2] == ['s3api', 'list-objects-v2']:
        stage = 'probe' if state['lists'] == 0 else 'verify'
    elif args[:2] == ['s3', 'cp']:
        stage = 'publish' if '--recursive' in args else 'packages'
    else:
        assert args[:2] == ['s3', 'rm']
        stage = 'delete'
    with (root / 'calls.jsonl').open('a') as out:
        out.write(json.dumps({'repo': repo, 'stage': stage, 'args': args}) + '\n')
    if os.environ.get('SYNC_TEST_FAIL') == stage:
        sys.exit(7)
    if stage in ('probe', 'verify'):
        prefix = args[args.index('--prefix') + 1]
        assert prefix == 'archlinux-mirror/' + repo + '/os/x86_64/'
        assert args[args.index('--delimiter') + 1] == '/'
        assert args[args.index('--output') + 1] == 'json'
        if stage == 'probe' and os.environ.get('SYNC_TEST_EMPTY_REMOTE'):
            sizes.clear()
        result = sizes.copy()
        if stage == 'verify' and os.environ.get('SYNC_TEST_MISSING'):
            result.pop(package + '.sig', None)
        if stage == 'verify' and os.environ.get('SYNC_TEST_CORRUPT_AFTER'):
            result[package] = 2
        # An empty bucket response omits Contents entirely.
        print(json.dumps({'Contents': [{'Key': prefix + name, 'Size': size} for name, size in result.items()]} if result else {}))
        state['lists'] += 1
    elif stage == 'packages':
        path = Path(args[2])
        sizes[path.name] = path.stat().st_size
        assert args[3].endswith('/' + path.name)
    elif stage == 'publish':
        for path in Path(args[2]).iterdir():
            sizes[path.name] = path.stat().st_size
    else:
        sizes.pop(args[2].rsplit('/', 1)[1], None)
    statefile.write_text(json.dumps(state))
'''
FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ['SYNC_TEST_ROOT'])
with (root / 'curl.jsonl').open('a') as out:
    out.write(json.dumps(sys.argv[-1]) + '\n')
if sys.argv[-1].endswith('/lastsync'):
    if os.environ.get('SYNC_TEST_FAIL') == 'lastsync-download':
        sys.exit(22)
    Path(sys.argv[sys.argv.index('--output') + 1]).write_text(os.environ.get('SYNC_TEST_LASTSYNC', '1792396800\n'))
    sys.exit(0)
if os.environ.get('SYNC_TEST_DOWNLOAD_FAIL') or (os.environ.get('SYNC_TEST_MIRROR_FAIL') and 'berkeley.edu' in sys.argv[-1]):
    sys.exit(22)
target = Path(sys.argv[sys.argv.index('--output') + 1])
data = b'signature' if target.name.endswith('.sig') else b'pkg'
target.write_bytes(b'bad' if os.environ.get('SYNC_TEST_BAD_DOWNLOAD') else data)
'''


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        binary = self.root / 'bin'
        binary.mkdir()
        for name, code in [('lftp', FAKE_LFTP), ('aws', FAKE_AWS), ('curl', FAKE_CURL)]:
            path = binary / name
            path.write_text(code)
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'],
                        SYNC_TEST_ROOT=str(self.root), AWS_ACCESS_KEY_ID='test-user',
                        AWS_SECRET_ACCESS_KEY='test-secret', BUCKET_NAME='test-bucket')
        for repo in ('core', 'multilib', 'extra'):
            directory = self.root / 'fixtures' / repo
            directory.mkdir(parents=True)
            for suffix in ('db', 'files'):
                self.database(directory / (repo + '.' + suffix))

    def database(self, path, filename='pkg-1-1-any.pkg.tar.zst'):
        signature = base64.b64encode(b'signature').decode()
        checksum = hashlib.sha256(b'pkg').hexdigest()
        content = f'%FILENAME%\n{filename}\n\n%CSIZE%\n3\n\n%SHA256SUM%\n{checksum}\n\n%PGPSIG%\n{signature}\n'.encode()
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

    def test_success_skips_matching_packages_publishes_then_removes_only_obsolete(self):
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('0 of 2 files need transfer', result.stdout)
        self.assertEqual((self.root / 'lastsync').read_text(), '1792396800\n')
        self.assertEqual(self.calls()[-1]['stage'], 'lastsync')
        for repo in ('core', 'multilib', 'extra'):
            calls = [call for call in self.calls() if call['repo'] == repo]
            self.assertEqual([c['stage'] for c in calls], ['metadata', 'probe', 'verify', 'publish', 'delete', 'delete', 'delete'])
            deleted = {c['args'][2].rsplit('/', 1)[1] for c in calls if c['stage'] == 'delete'}
            self.assertEqual(deleted, {repo + '.db.sig', 'old-1-1-any.pkg.tar.zst', 'old-1-1-any.pkg.tar.zst.sig'})

    def test_available_database_signatures_are_published_and_not_deleted(self):
        for repo in ('core', 'multilib', 'extra'):
            for suffix in ('db.sig', 'files.sig'):
                (self.root / 'fixtures' / repo / (repo + '.' + suffix)).write_bytes(b'database signature')
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        for repo in ('core', 'multilib', 'extra'):
            sizes = json.loads((self.root / (repo + '.json')).read_text())['sizes']
            for suffix in ('db.sig', 'files.sig'):
                self.assertEqual(sizes[repo + '.' + suffix], len(b'database signature'))
        self.assertFalse(any(c['stage'] == 'delete' and c['args'][2].endswith('.db.sig')
                             for c in self.calls()))

    def test_failures_are_not_reported_as_success_and_do_not_delete(self):
        for stage in ('metadata', 'probe', 'packages', 'verify', 'publish'):
            with self.subTest(stage=stage):
                for path in self.root.glob('*.json*'):
                    path.unlink()
                result = self.run_sync(SYNC_TEST_FAIL=stage, SYNC_TEST_REMOTE_SIZE='2')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('synchronization completed', result.stdout)
                self.assertNotIn('delete', [c['stage'] for c in self.calls()])
                self.assertFalse((self.root / 'lastsync').exists())
                if stage != 'publish':
                    self.assertNotIn('publish', [c['stage'] for c in self.calls()])

    def test_invalid_lastsync_blocks_sync(self):
        for value in ('', 'invalid\n', '123\n456\n'):
            with self.subTest(value=value):
                result = self.run_sync(SYNC_TEST_LASTSYNC=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Invalid upstream lastsync', result.stderr)
                self.assertEqual(self.calls(), [])

    def test_lastsync_download_failure_blocks_sync(self):
        result = self.run_sync(SYNC_TEST_FAIL='lastsync-download')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_lastsync_upload_failure_is_not_reported_as_success(self):
        result = self.run_sync(SYNC_TEST_FAIL='lastsync')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('synchronization completed', result.stdout)
        self.assertFalse((self.root / 'lastsync').exists())

    def test_truncated_package_is_selected_without_resending_signature(self):
        result = self.run_sync(SYNC_TEST_REMOTE_SIZE='2')
        self.assertEqual(result.returncode, 0, result.stderr)
        packages = [c for c in self.calls() if c['stage'] == 'packages']
        self.assertEqual(len(packages), 3)
        self.assertTrue(all(c['args'][3].endswith('/pkg-1-1-any.pkg.tar.zst') for c in packages))
        for repo in ('core', 'multilib', 'extra'):
            stages = [c['stage'] for c in self.calls() if c['repo'] == repo]
            self.assertLess(stages.index('packages'), stages.index('publish'))

    def test_empty_remote_directory_transfers_packages_and_signatures(self):
        result = self.run_sync(SYNC_TEST_EMPTY_REMOTE='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        packages = [c for c in self.calls() if c['stage'] == 'packages']
        self.assertEqual(len(packages), 6)
        self.assertEqual({Path(c['args'][2]).name for c in packages},
                         {'pkg-1-1-any.pkg.tar.zst', 'pkg-1-1-any.pkg.tar.zst.sig'})
        self.assertFalse(any(c['stage'] == 'delete' for c in self.calls()))

    def test_bad_download_blocks_upload_and_publication(self):
        result = self.run_sync(SYNC_TEST_EMPTY_REMOTE='1', SYNC_TEST_BAD_DOWNLOAD='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([c['stage'] for c in self.calls()], ['metadata', 'probe'])

    def test_download_failure_blocks_upload_and_publication(self):
        result = self.run_sync(SYNC_TEST_EMPTY_REMOTE='1', SYNC_TEST_DOWNLOAD_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([c['stage'] for c in self.calls()], ['metadata', 'probe'])

    def test_download_falls_back_to_another_mirror(self):
        result = self.run_sync(SYNC_TEST_EMPTY_REMOTE='1', SYNC_TEST_MIRROR_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        urls = (self.root / 'curl.jsonl').read_text()
        self.assertIn('berkeley.edu', urls)
        self.assertIn('frankfurt.mirror.pkgbuild.com', urls)

    def test_size_mismatch_after_upload_blocks_publication(self):
        result = self.run_sync(SYNC_TEST_REMOTE_SIZE='2', SYNC_TEST_CORRUPT_AFTER='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('incomplete packages', result.stderr)
        self.assertNotIn('publish', [c['stage'] for c in self.calls()])

    def test_missing_signature_blocks_publication_and_deletion(self):
        result = self.run_sync(SYNC_TEST_MISSING='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([c['stage'] for c in self.calls()], ['metadata', 'probe', 'verify'])

    def test_xz_packages_and_signatures_are_supported(self):
        package = 'sigrok-firmware-fx2lafw-0.1.7-1-any.pkg.tar.xz'
        for repo in ('core', 'multilib', 'extra'):
            for suffix in ('db', 'files'):
                self.database(self.root / 'fixtures' / repo / (repo + '.' + suffix), package)
        result = self.run_sync(SYNC_TEST_PACKAGE=package, SYNC_TEST_EMPTY_REMOTE='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c['stage'] == 'packages' and c['args'][3].endswith(package) for c in self.calls()))

    def test_invalid_or_inconsistent_snapshots_block_transfer(self):
        for filename in ('../../escape.pkg.tar.zst', 'other-1-1-any.pkg.tar.zst'):
            with self.subTest(filename=filename):
                (self.root / 'calls.jsonl').unlink(missing_ok=True)
                self.database(self.root / 'fixtures/core/core.files', filename)
                result = self.run_sync()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([c['stage'] for c in self.calls()], ['metadata'])

    def test_corrupt_database_blocks_transfer(self):
        (self.root / 'fixtures/core/core.db').write_text('broken archive')
        result = self.run_sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([c['stage'] for c in self.calls()], ['metadata'])

    def test_missing_credentials_and_unsafe_bucket_fail_before_network(self):
        for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'BUCKET_NAME'):
            with self.subTest(name=name):
                self.assertNotEqual(self.run_sync(**{name: ''}).returncode, 0)
        self.assertNotEqual(self.run_sync(BUCKET_NAME='../bad;bucket').returncode, 0)
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
