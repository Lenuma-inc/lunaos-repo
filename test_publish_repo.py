import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('publish_repo', Path(__file__).with_name('publish-repo.py'))
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = 'lunaos-repo'
        self.package = 'example-1-1-any.pkg.tar.zst'
        (self.root / self.package).write_bytes(b'package contents')
        (self.root / (self.package + '.sig')).write_text('valid signature')
        digest = hashlib.sha256((self.root / self.package).read_bytes()).hexdigest()
        description = f'%FILENAME%\n{self.package}\n\n%SHA256SUM%\n{digest}\n'.encode()
        for suffix in ('db', 'files'):
            with tarfile.open(self.root / (self.repo + '.' + suffix), 'w:gz') as archive:
                entry = tarfile.TarInfo('example-1-1/desc')
                entry.size = len(description)
                archive.addfile(entry, io.BytesIO(description))
            (self.root / (self.repo + '.' + suffix + '.sig')).write_text('database signature')
        self.hashes = publisher.repository_files(self.root, self.repo)
        self.metadata = {name for name in self.hashes if name.startswith(self.repo + '.')}
        self.commands = []
        self.assets = {name: {'id': index, 'digest': 'sha256:' + digest}
                       for index, (name, digest) in enumerate(self.hashes.items(), 1)}
        self.remote = {name: (self.root / name).read_bytes() for name in self.hashes}
        self.catalog = self.repo + '.sha256.json'
        self.remote[self.catalog] = json.dumps(self.hashes).encode()
        self.env = {'GH_REPO': 'Lenuma-inc/lunaos-repo', 'FTP_SERVER': 'ftp.example.org',
                    'FTP_USERNAME': 'test', 'FTP_PASSWORD': 'test', 'SERVER_DIR': 'bucket/lunaos-repo/'}

    def release_command(self, args, **kwargs):
        self.commands.append(args)
        if args[:3] == ['gh', 'api', '--paginate']:
            output = '\n'.join(f"{asset['id']}\t{name}\t{asset['digest']}" for name, asset in self.assets.items())
            return subprocess.CompletedProcess(args, 0, output + '\n', '')
        if args[:3] == ['gh', 'release', 'upload']:
            for path in map(Path, args[5:]):
                self.assets[path.name] = {'id': 100 + len(self.assets),
                                         'digest': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()}
        return subprocess.CompletedProcess(args, 0, '{"id": 10}', '')

    def ftp_command(self, args, **kwargs):
        commands = kwargs['input']
        self.commands.append(commands)
        if match := re.search(r'get "([^"]+)" -o "([^"]+)"', commands):
            if match[1] in self.remote:
                Path(match[2]).write_bytes(self.remote[match[1]])
        if match := re.search(r'mirror [^\n]+ "([^"]+)" "[^"]+"', commands):
            for path in Path(match[1]).iterdir():
                self.remote[path.name] = path.read_bytes()
        if match := re.search(r'cls -1 > "([^"]+)"', commands):
            Path(match[1]).write_text('\n'.join(self.remote) + '\n')
        for name in re.findall(r'^rm "([^"]+)"', commands, re.MULTILINE):
            self.remote.pop(name)
        if match := re.search(r'put "([^"]+)" -o "([^"]+)"', commands):
            self.remote[match[2]] = Path(match[1]).read_bytes()
        return subprocess.CompletedProcess(args, 0)

    def test_release_keeps_unchanged_assets_and_removes_orphans_after_metadata_upload(self):
        self.assets[self.repo + '.db']['digest'] = 'sha256:' + '0' * 64
        self.assets['orphan.pkg.tar.zst.sig'] = {'id': 999, 'digest': 'sha256:' + '1' * 64}
        with patch.dict(os.environ, self.env), patch.object(publisher.subprocess, 'run', side_effect=self.release_command):
            publisher.publish_release(self.root, self.repo, self.hashes, self.metadata)
        uploads = [args for args in self.commands if args[:3] == ['gh', 'release', 'upload']]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0][5:], [str(self.root / (self.repo + '.db'))])
        deletion = next(index for index, args in enumerate(self.commands) if 'DELETE' in args)
        self.assertGreater(deletion, self.commands.index(uploads[0]))

    def test_release_upload_failure_blocks_metadata_and_deletion(self):
        self.assets[self.package]['digest'] = 'sha256:' + '0' * 64
        self.assets[self.repo + '.db']['digest'] = 'sha256:' + '0' * 64
        def command(args, **kwargs):
            if args[:3] == ['gh', 'release', 'upload']:
                self.commands.append(args)
                raise subprocess.CalledProcessError(1, args)
            return self.release_command(args, **kwargs)
        with patch.dict(os.environ, self.env), patch.object(publisher.subprocess, 'run', side_effect=command):
            with self.assertRaises(subprocess.CalledProcessError):
                publisher.publish_release(self.root, self.repo, self.hashes, self.metadata)
        uploads = [args for args in self.commands if args[:3] == ['gh', 'release', 'upload']]
        self.assertEqual(uploads[0][5:], [str(self.root / self.package)])
        self.assertFalse(any('DELETE' in args for args in self.commands))

    def test_release_digest_mismatch_blocks_metadata_publication(self):
        self.assets[self.package]['digest'] = 'sha256:' + '0' * 64
        self.assets[self.repo + '.db']['digest'] = 'sha256:' + '0' * 64
        def command(args, **kwargs):
            if args[:3] == ['gh', 'release', 'upload']:
                self.commands.append(args)
                return subprocess.CompletedProcess(args, 0)
            return self.release_command(args, **kwargs)
        with patch.dict(os.environ, self.env), patch.object(publisher.subprocess, 'run', side_effect=command):
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                publisher.publish_release(self.root, self.repo, self.hashes, self.metadata)
        self.assertEqual(sum(args[:3] == ['gh', 'release', 'upload'] for args in self.commands), 1)

    def test_release_asset_deletion_retains_bounded_retries(self):
        with (patch.object(publisher.subprocess, 'run', side_effect=[subprocess.CalledProcessError(1, ['gh']),
                                                                  subprocess.CompletedProcess(['gh'], 0)]) as command,
              patch.object(publisher.time, 'sleep') as sleep):
            publisher.delete_release_asset('Lenuma-inc/lunaos-repo', 999)
        self.assertEqual(command.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_s3_keeps_unchanged_files_and_uploads_catalog_after_cleanup(self):
        self.remote['orphan.pkg.tar.zst.sig'] = b'old'
        with patch.dict(os.environ, self.env), patch.object(publisher.subprocess, 'run', side_effect=self.ftp_command):
            publisher.publish_s3(self.root, self.repo, self.hashes, self.metadata)
        self.assertFalse(any('mirror ' in commands for commands in self.commands))
        self.assertNotIn('orphan.pkg.tar.zst.sig', self.remote)
        self.assertLess(self.commands[-1].index('rm '), self.commands[-1].index('put '))
        self.assertEqual(json.loads(self.remote[self.catalog]), self.hashes)

    def test_s3_missing_files_and_same_size_changes_are_uploaded_before_metadata(self):
        self.remote.pop(self.package)
        old = dict(self.hashes)
        old[self.package + '.sig'] = '0' * 64
        old[self.repo + '.db'] = '1' * 64
        self.remote[self.catalog] = json.dumps(old).encode()
        with patch.dict(os.environ, self.env), patch.object(publisher.subprocess, 'run', side_effect=self.ftp_command):
            publisher.publish_s3(self.root, self.repo, self.hashes, self.metadata)
        mirrors = [command for command in self.commands if 'mirror ' in command]
        self.assertEqual(len(mirrors), 2)
        self.assertIn('/packages"', mirrors[0])
        self.assertIn('/databases"', mirrors[1])
        self.assertEqual(self.remote[self.package], (self.root / self.package).read_bytes())

    def test_s3_incomplete_upload_does_not_publish_metadata_or_catalog(self):
        self.remote.pop(self.package)
        def command(args, **kwargs):
            if 'mirror ' in kwargs['input']:
                commands = re.sub(r'mirror [^\n]+\n', '', kwargs['input'])
                return self.ftp_command(args, **(kwargs | {'input': commands}))
            return self.ftp_command(args, **kwargs)
        with patch.dict(os.environ, self.env), patch.object(publisher.subprocess, 'run', side_effect=command):
            with self.assertRaisesRegex(RuntimeError, 'FTP verification failed'):
                publisher.publish_s3(self.root, self.repo, self.hashes, self.metadata)
        self.assertFalse(any('/databases"' in commands or 'put ' in commands for commands in self.commands))

    def test_s3_first_upload_without_catalog_transfers_all_payloads(self):
        self.remote.pop(self.catalog)
        with patch.dict(os.environ, self.env), patch.object(publisher.subprocess, 'run', side_effect=self.ftp_command):
            publisher.publish_s3(self.root, self.repo, self.hashes, self.metadata)
        self.assertEqual(sum('mirror ' in commands for commands in self.commands), 2)

    def test_signing_preserves_valid_signatures_and_repairs_invalid_ones(self):
        binary = self.root / "bin"
        binary.mkdir()
        gpg = binary / "gpg"
        gpg.write_text("""#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
if '--verify' in args:
    sys.exit(0 if Path(args[-2]).read_text() == 'valid signature' else 1)
Path(args[args.index('--output') + 1]).write_text('new signature')
""")
        gpg.chmod(0o755)
        env = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'],
                   PKGDEST=str(self.root), GPG_PASSPHRASE='test-passphrase')
        script = Path(__file__).with_name('sign.sh')
        subprocess.run(['sh', str(script)], env=env, check=True)
        signature = self.root / (self.package + '.sig')
        self.assertEqual(signature.read_text(), 'valid signature')
        signature.write_text('invalid signature')
        subprocess.run(['sh', str(script)], env=env, check=True)
        self.assertEqual(signature.read_text(), 'new signature')

    def test_inventory_rejects_inconsistent_file_database(self):
        with tarfile.open(self.root / (self.repo + '.files'), 'w:gz') as archive:
            content = ('%FILENAME%\nother-1-1-any.pkg.tar.zst\n%SHA256SUM%\n' + '0' * 64).encode()
            entry = tarfile.TarInfo('other-1-1/desc')
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        with self.assertRaisesRegex(ValueError, 'databases do not match'):
            publisher.repository_files(self.root, self.repo)

    def test_inventory_rejects_missing_mismatched_or_orphaned_package_files(self):
        for scenario in ('missing-signature', 'wrong-hash', 'orphan-signature'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                for source in self.root.iterdir():
                    (root / source.name).write_bytes(source.read_bytes())
                if scenario == 'missing-signature':
                    (root / (self.package + '.sig')).unlink()
                elif scenario == 'wrong-hash':
                    (root / self.package).write_bytes(b'changed contents')
                else:
                    (root / 'old.pkg.tar.zst.sig').write_text('orphan')
                with self.assertRaises(ValueError):
                    publisher.repository_files(root, self.repo)


if __name__ == '__main__':
    unittest.main()
