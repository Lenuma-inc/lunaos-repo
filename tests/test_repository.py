"""Exercise actual Arch archives, GPG signing and database generation in temp dirs.

Never installs packages or accesses the network. Requires an unprivileged user
and makepkg/fakeroot/bsdtar/repo-add/gpg on the test machine.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ('makepkg', 'fakeroot', 'bsdtar', 'repo-add', 'gpg', 'gpgconf', 'zstd')


@unittest.skipUnless(os.geteuid() != 0 and all(shutil.which(tool) for tool in TOOLS),
                     'requires Arch tools and an unprivileged user')
class RepositoryIntegration(unittest.TestCase):
    def test_real_build_sign_and_reindex_from_different_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packages, source, home = root / 'repo', root / 'source', root / 'gnupg'
            for path in (packages, source, home):
                path.mkdir(mode=0o700)
            env = dict(os.environ, PKGDEST=str(packages), GNUPGHOME=str(home),
                       GPG_PASSPHRASE='test-key-passphrase', repo='fixture', LC_ALL='C')

            def run(args, cwd=root):
                result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            try:
                run(['gpg', '--batch', '--pinentry-mode', 'loopback', '--passphrase', env['GPG_PASSPHRASE'],
                     '--quick-generate-key', 'Buildbot Test <fixture@example.invalid>', 'ed25519', 'sign', '0'])
                for name in ('fixture', 'obsolete'):
                    (source / 'PKGBUILD').write_text(f'''pkgname={name}
pkgver=1
pkgrel=1
arch=(any)
pkgdesc='Local buildbot regression fixture'
license=(MIT)
package() {{
    install -dm755 "$pkgdir/usr/share/{name}"
    printf 'fixture\\n' > "$pkgdir/usr/share/{name}/data"
}}
''')
                    run(['makepkg', '--noconfirm', '--nodeps', '--nocheck', '--nosign', '--cleanbuild'], cwd=source)
                run(['sh', str(ROOT / 'sign.sh')])
                run(['sh', str(ROOT / 'update-repo.sh')])
                listing = run(['bsdtar', '-tf', str(packages / 'fixture.db.tar.gz')])
                self.assertIn('fixture-1-1/desc', listing)
                self.assertIn('obsolete-1-1/desc', listing)
                for kind in ('db', 'files'):
                    self.assertTrue((packages / f'fixture.{kind}').exists())
                    run(['gpg', '--batch', '--verify', str(packages / f'fixture.{kind}.sig'), str(packages / f'fixture.{kind}')])
                # Removed split packages must disappear from a rebuilt database.
                for archive in packages.glob('obsolete-*'):
                    archive.unlink()
                run(['sh', str(ROOT / 'update-repo.sh')])
                listing = run(['bsdtar', '-tf', str(packages / 'fixture.db')])
                self.assertNotIn('obsolete', listing)
                self.assertIn('fixture-1-1/desc', listing)
                # A signing failure must leave the previously generated DB untouched.
                before = (packages / 'fixture.db.tar.gz').read_bytes()
                fake_bin = root / 'bin'
                fake_bin.mkdir()
                fake_gpg = fake_bin / 'gpg'
                fake_gpg.write_text('#!/bin/sh\necho "simulated GPG failure" >&2\nexit 9\n')
                fake_gpg.chmod(0o755)
                env['PATH'] = str(fake_bin) + os.pathsep + env['PATH']
                result = subprocess.run(['sh', str(ROOT / 'update-repo.sh')], cwd=root, env=env, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((packages / 'fixture.db.tar.gz').read_bytes(), before)
                self.assertFalse(list(packages.glob('.database.*')))
            finally:
                subprocess.run(['gpgconf', '--homedir', str(home), '--kill', 'gpg-agent'], capture_output=True)


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'calls.jsonl'
        self.env = dict(os.environ, PKGDEST=str(self.repo), repo='fixture',
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'], GH_CALLS=str(self.log))
        for name in ['a.pkg.tar.zst', 'a.pkg.tar.zst.sig', 'fixture.db', 'fixture.db.sig',
                     'fixture.db.tar.gz', 'fixture.db.tar.gz.sig', 'fixture.files', 'fixture.files.sig',
                     'fixture.files.tar.gz', 'fixture.files.tar.gz.sig']:
            (self.repo / name).write_text('fixture')
        gh = self.bin / 'gh'
        gh.write_text('''#!/usr/bin/env python3
import json,os,sys
with open(os.environ['GH_CALLS'],'a') as f:
    f.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1:3] == ['release','upload'] and os.environ.get('FAIL_UPLOAD'):
    sys.exit(1)
if '--json' in sys.argv:
    print('old.pkg.tar.zst\\nold.pkg.tar.zst.sig\\nunrelated.txt\\na.pkg.tar.zst')
''')
        gh.chmod(0o755)

    def run_script(self):
        return subprocess.run(['bash', str(ROOT / 'publish-repo.sh')], cwd=self.root,
                              env=self.env, text=True, capture_output=True, timeout=10)

    def test_packages_precede_database_and_cleanup_is_last(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        uploads = [call for call in calls if call[:2] == ['release', 'upload']]
        self.assertIn('a.pkg.tar.zst', uploads[0])
        self.assertIn('fixture.db', uploads[1])
        deleted = [call[3] for call in calls if call[:2] == ['release', 'delete-asset']]
        self.assertEqual(deleted, ['old.pkg.tar.zst', 'old.pkg.tar.zst.sig'])

    def test_failed_upload_prevents_database_and_deletions(self):
        self.env['FAIL_UPLOAD'] = '1'
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertFalse(any(call[:2] == ['release', 'delete-asset'] for call in calls))
        self.assertFalse(any('fixture.db' in call for call in calls))


@unittest.skipUnless(shutil.which('lftp'), 'requires lftp')
class MirrorIntegration(unittest.TestCase):
    def test_real_mirror_uploads_packages_before_database_and_deletes_last(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / 'source', root / 'target'
            source.mkdir()
            target.mkdir()
            (source / 'a.pkg.tar.zst').write_text('package')
            (source / 'fixture.db').write_text('database')
            (target / 'obsolete.pkg.tar.zst').write_text('old')
            password = 'punctuation,:"$secret'
            env = dict(os.environ, LOCAL_DIR=str(source) + '/', SERVER_DIR=str(target),
                       FTP_SERVER='file:///', FTP_USERNAME='fixture', FTP_PASSWORD=password)
            result = subprocess.run(['bash', str(ROOT / 'upload-repo.sh')], cwd=root,
                                    env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((target / 'a.pkg.tar.zst').read_text(), 'package')
            self.assertEqual((target / 'fixture.db').read_text(), 'database')
            self.assertFalse((target / 'obsolete.pkg.tar.zst').exists())
            log = result.stdout + result.stderr
            self.assertLess(log.index('a.pkg.tar.zst'), log.index('fixture.db'))
            self.assertLess(log.index('fixture.db'), log.index('obsolete.pkg.tar.zst'))
            self.assertNotIn(password, log)


if __name__ == '__main__':
    unittest.main()
