import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import buildbot as bot
from buildbot_runtime import CommandError, Runtime, atomic_json


class BotCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for key, name in [('ROOT', 'root'), ('WORK', 'work'), ('PKGDEST', 'repo'), ('LOGDIR', 'logs')]:
            path = self.root / name
            path.mkdir()
            p = patch.object(bot, key, path)
            p.start()
            self.addCleanup(p.stop)
        self.state = self.root / 'state.json'
        p = patch.object(bot, 'STATE_PATH', self.state)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(bot, 'RUNTIME', Runtime(bot.LOGDIR))
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'BUILDBOT_ISSUES': 'false', 'BUILDBOT_PACKAGES': '',
                                  'BUILDBOT_FULL': 'false', 'BUILDBOT_ARCH_ISSUES': 'false', 'GITHUB_OUTPUT': str(self.root / 'output'),
                                  'GITHUB_STEP_SUMMARY': str(self.root / 'summary')})
        p.start()
        self.addCleanup(p.stop)
        self.capture = contextlib.redirect_stdout(io.StringIO())
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def metadata(self, name, deps=()):
        return {'names': [name], 'deps': list(deps), 'provides': []}

    def manifest(self, names):
        return {name: {'directory': name, 'url': f'https://example.org/{name}.git', 'ref': 'HEAD',
                       'makepkg_flags': '-s', 'before': '', 'after': ''} for name in names}

    def saved(self, names, versions=None):
        atomic_json(self.state, {'sources': {name: 'old' for name in names},
                               'packages': {name: {'status': 'built', 'metadata': self.metadata(name),
                                                  'dependency_versions': versions or {}} for name in names}})

    def harness(self, packages, metadata=None, failed=(), source_failed=(), stale=(), hook_failed=(), revisions=None):
        metadata = metadata or {name: self.metadata(name) for name in packages}
        calls = []
        published = {}
        for name in packages:
            archive = bot.PKGDEST / f'{name}-1-1-any.pkg.tar.zst'
            archive.write_text('old package')
            published[name] = {'pkgname': name, 'pkgbase': name, 'pkgver': '1-1', 'arch': 'any', '_path': str(archive)}

        def clone(row, expected=None):
            path = bot.WORK / row['directory']
            path.mkdir(exist_ok=True)
            return path, expected or 'new'

        def source(row):
            if row['directory'] in source_failed:
                raise RuntimeError('source offline')
            return (revisions or {}).get(row['directory'], 'new')

        def run(args, **kwargs):
            calls.append(args)
            if args[:2] == ['pacman', '-Sl']:
                return 'core python 3.14.2-1'
            if args[:2] == ['pacman', '-Q']:
                return 'python 3.14.2-1'
            if args[0] == 'vercmp':
                return '0' if args[1] == args[2] else '1'
            if args[0] == 'bash' and Path(kwargs['cwd']).name in hook_failed:
                raise RuntimeError('hook failed')
            if args[:4] == ['runuser', '-u', 'user', '--']:
                name = Path(kwargs['cwd']).name
                if name in failed:
                    raise RuntimeError('compiler failed')
                version = '1-1' if name in stale else '2-1'
                target = Path(kwargs['env']['PKGDEST']) / f'{name}-{version}-any.pkg.tar.zst'
                target.write_text(json.dumps({'pkgname': name, 'pkgbase': name, 'pkgver': version, 'arch': 'any'}))
            return ''

        for function, replacement in [('read_manifest', lambda: packages), ('source_sha', source), ('clone', clone),
                                     ('package_info', lambda path: metadata[path.name]), ('run', run),
                                     ('published_packages', lambda: dict(published)),
                                     ('archive_info', lambda path: json.loads(path.read_text()))]:
            p = patch.object(bot, function, replacement)
            p.start()
            self.addCleanup(p.stop)
        return calls

    def test_cold_cache_matching_release_does_not_build_or_request_pkgrel(self):
        packages = self.manifest(['a', 'b'])
        metadata = {name: dict(self.metadata(name), version='1-1') for name in packages}
        calls = self.harness(packages, metadata)
        with patch.object(bot, 'create_pkgrel_issue') as issue:
            bot.main()
        self.assertFalse(any(args[0] == 'runuser' for args in calls))
        issue.assert_not_called()
        self.assertEqual(json.loads((bot.LOGDIR / 'plan.json').read_text())['order'], [])
        self.assertTrue(all(item['status'] == 'up-to-date' for item in json.loads((bot.LOGDIR / 'results.json').read_text()).values()))
        self.assertEqual(json.loads(self.state.read_text())['sources'], {'a': 'new', 'b': 'new'})

    def test_same_version_source_change_is_not_compiled_to_discover_pkgrel(self):
        self.saved(['a'])
        state = json.loads(self.state.read_text())
        state['packages']['a']['metadata']['version'] = '1-1'
        atomic_json(self.state, state)
        calls = self.harness(self.manifest(['a']), {'a': dict(self.metadata('a'), version='1-1')})
        with patch.object(bot, 'create_pkgrel_issue') as issue:
            bot.main()
        issue.assert_not_called()
        self.assertFalse(any(args[0] == 'runuser' for args in calls))
        self.assertEqual(json.loads(self.state.read_text())['packages']['a']['status'], 'needs-pkgrel')

    def test_release_newer_than_cache_does_not_cause_pkgrel_issue(self):
        self.saved(['a'])
        state = json.loads(self.state.read_text())
        state['packages']['a']['metadata']['version'] = '0-1'
        atomic_json(self.state, state)
        calls = self.harness(self.manifest(['a']), {'a': dict(self.metadata('a'), version='1-1')})
        with patch.dict(os.environ, {'BUILDBOT_ISSUES': 'true'}), patch.object(bot, 'create_pkgrel_issue') as issue:
            bot.main()
        issue.assert_not_called()
        self.assertFalse(any(args[0] == 'runuser' for args in calls))
        self.assertEqual(json.loads(self.state.read_text())['packages']['a']['status'], 'up-to-date')

    def test_commit_with_identical_tree_does_not_build_or_raise_issue(self):
        self.saved(['a'])
        state = json.loads(self.state.read_text())
        state['packages']['a']['metadata']['tree'] = 'same-tree'
        atomic_json(self.state, state)
        calls = self.harness(self.manifest(['a']), {'a': dict(self.metadata('a'), version='1-1', tree='same-tree')})
        bot.main()
        self.assertFalse(any(args[0] == 'runuser' for args in calls))
        self.assertEqual(json.loads(self.state.read_text())['packages']['a']['status'], 'up-to-date')

    def test_manual_same_version_build_does_not_create_false_pkgrel_issue(self):
        self.saved(['a'])
        calls = self.harness(self.manifest(['a']), stale={'a'}, revisions={'a': 'old'})
        with patch.dict(os.environ, {'BUILDBOT_FULL': 'true', 'BUILDBOT_ISSUES': 'true'}), patch.object(bot, 'create_pkgrel_issue') as issue:
            bot.main()
        self.assertTrue(any(args[0] == 'runuser' for args in calls))
        issue.assert_not_called()
        self.assertEqual(json.loads(self.state.read_text())['packages']['a']['status'], 'unchanged')

    def test_runtime_only_metapackage_does_not_rebuild_after_dependency(self):
        packages = self.manifest(['lib', 'meta'])
        self.saved(packages)
        state = json.loads(self.state.read_text())
        state['packages']['meta']['metadata'].update(deps=['lib'], architectures=['any'], build_deps=[])
        atomic_json(self.state, state)
        self.harness(packages, revisions={'meta': 'old'})
        bot.main()
        self.assertEqual(json.loads((bot.LOGDIR / 'plan.json').read_text())['order'], ['lib'])

    def test_untracked_dependency_patch_does_not_rebuild(self):
        self.saved(['a'], {'gcc': '13.1'})
        state = json.loads(self.state.read_text())
        state['packages']['a']['metadata']['deps'] = ['gcc']
        atomic_json(self.state, state)
        self.harness(self.manifest(['a']), revisions={'a': 'old'})
        with patch.object(bot, 'available_versions', return_value={'gcc': '13.2'}):
            bot.main()
        self.assertEqual(json.loads((bot.LOGDIR / 'plan.json').read_text())['order'], [])

    def test_closed_issue_for_same_revision_is_not_recreated(self):
        issue = {'number': 4, 'title': '[rebuild] a: bump pkgrel', 'state': 'CLOSED', 'body': 'Source revision: `revision`'}
        with patch.object(bot, 'run', return_value=json.dumps([issue])) as run:
            number = bot.create_pkgrel_issue('a', self.manifest(['a'])['a'], 'revision', '1-1', '1-1', {})
        self.assertEqual(number, 4)
        self.assertEqual(run.call_count, 1)

    def test_arch_pkgrel_and_epoch_differences_are_not_upstream_updates(self):
        packages = self.manifest(['a'])
        packages['a']['url'] = 'https://gitlab.com/LunaOS/a.git'
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[0] == 'gh':
                return '[]'
            if args[0] == 'pacman':
                return 'Version : 2:1.0-9\nArchitecture : any'
            return '0'
        with patch.object(bot, 'run', run):
            bot.check_arch_updates(packages, {'a': {'pkgver': '1.0-1'}}, {})
        self.assertIn(['vercmp', '1.0', '1.0'], calls)
        self.assertFalse(any(args[:3] == ['gh', 'issue', 'create'] for args in calls))

    def test_aur_packages_do_not_generate_arch_fork_issues(self):
        packages = self.manifest(['a'])
        packages['a']['url'] = 'https://github.com/archlinux/aur.git'
        with patch.object(bot, 'run', return_value='[]') as run:
            bot.check_arch_updates(packages, {'a': {'pkgver': '1.0-1'}}, {})
        self.assertEqual(run.call_count, 1)

    def test_cycles_order_without_mutating_graph(self):
        dependencies = {'a': {'b'}, 'b': {'a'}, 'c': {'b'}, 'independent': set()}
        original = {k: set(v) for k, v in dependencies.items()}
        result = bot.order(set(dependencies), dependencies)
        self.assertEqual(set(result), set(dependencies))
        self.assertEqual(dependencies, original)
        self.assertLess(result.index('b'), result.index('c'))
        self.assertIn('bootstrap-cycle', (bot.LOGDIR / 'buildbot.log').read_text())

    def test_clone_pins_observed_revision_before_changing_ownership(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[:2] == ['git', 'clone']:
                Path(args[-1]).mkdir()
            if args[:2] == ['git', 'rev-parse']:
                return 'expected' if any(call[:2] == ['git', 'checkout'] for call in calls) else 'moved'
            return ''
        with patch.object(bot, 'run', run):
            _, revision = bot.clone(self.manifest(['a'])['a'], 'expected')
        self.assertEqual(revision, 'expected')
        self.assertEqual(calls[-1][0], 'chown')
        self.assertIn(['git', 'fetch', '--depth', '1', 'origin', 'expected'], calls)

    def test_uninstalled_dependency_upgrade_triggers_rebuild(self):
        self.saved(['a'], {'python': '3.13'})
        state = json.loads(self.state.read_text())
        state['packages']['a']['metadata']['deps'] = ['python']
        atomic_json(self.state, state)
        self.harness(self.manifest(['a']), revisions={'a': 'old'})
        with patch.object(bot, 'installed_versions', return_value={}):
            bot.main()
        plan = json.loads((bot.LOGDIR / 'plan.json').read_text())
        self.assertEqual(plan['order'], ['a'])
        self.assertEqual(plan['dependency_changes']['a']['python'], ['3.13', '3.14'])

    def test_run_budget_defers_packages_and_records_retry_state(self):
        self.saved(['a'])
        self.harness(self.manifest(['a']))
        with patch.dict(os.environ, {'BUILDBOT_RUN_TIMEOUT': '0'}):
            bot.main()
        self.assertEqual(json.loads(self.state.read_text())['packages']['a']['status'], 'deferred')
        self.assertTrue((bot.PKGDEST / 'a-1-1-any.pkg.tar.zst').exists())
        self.assertIn('deferred=1', (self.root / 'output').read_text())

    def test_interrupt_preserves_checkpoint_and_original_exception(self):
        self.harness(self.manifest(['a']))
        original = bot.run
        def interrupted(args, **kwargs):
            if args[0] == 'runuser':
                raise KeyboardInterrupt('test cancellation')
            return original(args, **kwargs)
        with patch.object(bot, 'run', interrupted):
            with self.assertRaisesRegex(KeyboardInterrupt, 'test cancellation'):
                bot.main()
        self.assertEqual(json.loads(self.state.read_text())['packages']['a']['status'], 'failed')
        self.assertTrue((bot.PKGDEST / 'a-1-1-any.pkg.tar.zst').exists())

    def test_graph_ignores_removed_cached_packages(self):
        deps, _ = bot.graph({'a': {}}, {'a': self.metadata('a'), 'removed': self.metadata('removed', ['a'])})
        self.assertEqual(deps, {'a': set()})

    def test_graph_exact_names_take_priority_over_virtual_providers(self):
        data = {'a': self.metadata('a', ['lib']), 'lib': self.metadata('lib'),
                'other': {'names': ['other'], 'deps': [], 'provides': ['lib']}}
        deps, _ = bot.graph(self.manifest(data), data)
        self.assertEqual(deps['a'], {'lib'})

    def test_architecture_dependencies_filtered(self):
        (bot.WORK / '.SRCINFO').write_text('pkgname = test\ndepends = common\ndepends_x86_64 = good>=1\ndepends_aarch64 = wrong\nprovides_aarch64 = wrong-virtual\n')
        self.assertEqual(bot.srcinfo(bot.WORK), {'names': ['test'], 'deps': ['common', 'good'], 'provides': [], 'build_deps': [], 'architectures': [], 'dynamic_version': False})

    def test_pkgbuild_metadata_overrides_stale_srcinfo(self):
        (bot.WORK / 'PKGBUILD').write_text('pkgname=test\npkgver=2\npkgrel=1\n')
        (bot.WORK / '.SRCINFO').write_text('pkgname = test\npkgver = 1\npkgrel = 1\n')
        with patch.object(bot, 'run', return_value='pkgname = test\npkgver = 2\npkgrel = 1\n'):
            self.assertEqual(bot.srcinfo(bot.WORK)['version'], '2-1')

    def test_empty_srcinfo_rejected(self):
        (bot.WORK / '.SRCINFO').write_text('pkgbase = empty\n')
        with self.assertRaisesRegex(RuntimeError, 'no package names'):
            bot.srcinfo(bot.WORK)

    def test_python_abi_normalization(self):
        self.assertEqual(bot.dependency_versions(self.metadata('a', ['python']), {'python': '2:3.14.2-1'}), {'python': '3.14'})

    def test_removed_packages_are_pruned_from_state(self):
        self.saved(['a', 'removed'])
        sources, previous = bot.load_state({'a': {}})
        self.assertEqual(set(sources), {'a'})
        self.assertEqual(set(previous), {'a'})

    def test_corrupt_state_recovers(self):
        self.state.write_text('{broken')
        self.assertEqual(bot.load_state({'a': {}}), ({}, {}))
        self.assertIn('state-invalid', (bot.LOGDIR / 'buildbot.log').read_text())

    def test_invalid_metadata_recovers(self):
        self.state.write_text(json.dumps({'packages': {'a': {'metadata': {'names': 'bad'}}}}))
        self.assertEqual(bot.load_state({'a': {}}), ({}, {}))

    def test_manifest_rejects_traversal_and_missing_columns(self):
        header = 'directory\turl\tref\tmakepkg_flags\tbefore\tafter\n'
        for row in ['../escape\thttps://example.org\tHEAD\t-s\t\t\n', 'a\thttps://example.org\tHEAD\n']:
            (bot.ROOT / 'packages.tsv').write_text(header + row)
            with self.assertRaises(RuntimeError):
                bot.read_manifest()

    def test_install_flags_are_deferred(self):
        self.assertEqual(bot.makepkg_options('-si -d --install'), (['-s', '-d'], True))
        self.assertEqual(bot.makepkg_options('-i'), ([], True))

    def test_failed_dependency_blocks_only_its_consumers(self):
        packages = self.manifest(['a', 'b', 'independent'])
        metadata = {name: self.metadata(name, ['a'] if name == 'b' else []) for name in packages}
        self.harness(packages, metadata, failed={'a'})
        self.assertEqual(bot.main(), 0)
        results = json.loads((bot.LOGDIR / 'results.json').read_text())
        self.assertEqual({k: v['status'] for k, v in results.items()}, {'a': 'failed', 'b': 'blocked', 'independent': 'built'})
        self.assertIn('failed=2', (self.root / 'output').read_text())
        self.assertTrue((bot.PKGDEST / 'a-1-1-any.pkg.tar.zst').exists())
        self.assertFalse((bot.PKGDEST / 'independent-1-1-any.pkg.tar.zst').exists())
        self.assertTrue((bot.PKGDEST / 'independent-2-1-any.pkg.tar.zst').exists())

    def test_source_failure_uses_cached_graph_to_block_dependents(self):
        packages = self.manifest(['a', 'b'])
        self.saved(packages)
        state = json.loads(self.state.read_text())
        state['packages']['b']['metadata']['deps'] = ['a']
        atomic_json(self.state, state)
        self.harness(packages, source_failed={'a'}, revisions={'b': 'old'})
        bot.main()
        results = json.loads((bot.LOGDIR / 'results.json').read_text())
        self.assertEqual(results['a']['status'], 'source-check-failed')
        self.assertEqual(results['b']['status'], 'blocked')

    def test_missing_metadata_does_not_force_all_packages(self):
        packages = self.manifest(['a', 'b'])
        self.saved(['b'])
        calls = self.harness(packages, revisions={'b': 'old'})
        bot.main()
        built = [args for args in calls if args[0] == 'runuser']
        self.assertEqual(len(built), 1)
        self.assertEqual(json.loads((bot.LOGDIR / 'plan.json').read_text())['order'], ['a'])

    def test_removed_retry_entry_cannot_crash_planner(self):
        self.saved(['a', 'removed'])
        state = json.loads(self.state.read_text())
        state['packages']['removed']['status'] = 'failed'
        atomic_json(self.state, state)
        self.harness(self.manifest(['a']), revisions={'a': 'old'})
        bot.main()
        self.assertEqual(json.loads((bot.LOGDIR / 'plan.json').read_text())['order'], [])

    def test_stale_build_preserves_log_source_and_published_archive(self):
        self.saved(['a'])
        self.harness(self.manifest(['a']), stale={'a'})
        bot.main()
        self.assertEqual(json.loads(self.state.read_text())['sources']['a'], 'old')
        self.assertEqual(json.loads(self.state.read_text())['packages']['a']['pending_source'], 'new')
        log = (bot.LOGDIR / 'a.log').read_text()
        self.assertIn('build-start', log)
        self.assertIn('needs-pkgrel', log)
        self.assertEqual((bot.PKGDEST / 'a-1-1-any.pkg.tar.zst').read_text(), 'old package')

    def test_optional_issue_failure_does_not_turn_stale_package_into_build_failure(self):
        self.saved(['a'])
        self.harness(self.manifest(['a']), stale={'a'})
        with patch.dict(os.environ, {'BUILDBOT_ISSUES': 'true'}), patch.object(bot, 'check_arch_updates'), \
             patch.object(bot, 'create_pkgrel_issue', side_effect=RuntimeError('API offline')):
            bot.main()
        results = json.loads((bot.LOGDIR / 'results.json').read_text())
        self.assertEqual(results['a']['status'], 'needs-pkgrel')
        self.assertIn('API offline', (bot.LOGDIR / 'a.log').read_text())

    def test_after_hook_failure_preserves_old_archive(self):
        packages = self.manifest(['a'])
        packages['a']['after'] = 'fail'
        self.harness(packages, hook_failed={'a'})
        bot.main()
        self.assertEqual((bot.PKGDEST / 'a-1-1-any.pkg.tar.zst').read_text(), 'old package')
        self.assertFalse((bot.PKGDEST / 'a-2-1-any.pkg.tar.zst').exists())
        self.assertEqual(json.loads(self.state.read_text())['packages']['a']['status'], 'failed')

    def test_issue_pending_source_is_not_rebuilt_on_every_run(self):
        self.saved(['a'])
        state = json.loads(self.state.read_text())
        state['packages']['a'].update(status='needs-pkgrel', pending_source='new')
        atomic_json(self.state, state)
        self.harness(self.manifest(['a']))
        bot.main()
        self.assertEqual(json.loads((bot.LOGDIR / 'plan.json').read_text())['order'], [])

    def test_missing_firmware_is_explicit_failure(self):
        self.harness(self.manifest(['linux-firmware-apple']))
        with patch.dict(os.environ, {'FIRMWARE_TARBALL': str(self.root / 'missing.tar')}):
            bot.main()
        result = json.loads((bot.LOGDIR / 'results.json').read_text())['linux-firmware-apple']
        self.assertEqual(result['status'], 'failed')
        self.assertIn('firmware archive is missing', result['detail'])

    def test_fatal_error_sets_safe_outputs_and_traceback(self):
        with patch.object(bot, 'main', side_effect=RuntimeError('fatal fixture')):
            self.assertEqual(bot.entrypoint(), 1)
        output = (self.root / 'output').read_text()
        self.assertIn('fatal=1', output)
        self.assertIn('packages_built=0', output)
        self.assertIn('Traceback', (bot.LOGDIR / 'buildbot.log').read_text())

    def test_run_without_github_output_reports_failure(self):
        self.harness(self.manifest(['a']), failed={'a'})
        os.environ.pop('GITHUB_OUTPUT')
        self.assertEqual(bot.main(), 1)

    def test_promote_rolls_back_when_second_copy_fails(self):
        stage = self.root / 'stage'
        stage.mkdir()
        old = bot.PKGDEST / 'a-old.pkg.tar.zst'
        old.write_text('previous')
        old.with_name(old.name + '.sig').write_text('signature')
        built = [stage / 'a-new.pkg.tar.zst', stage / 'b-new.pkg.tar.zst']
        for item in built:
            item.write_text('new')
        infos = [{'pkgname': n, 'pkgbase': 'a', 'pkgver': '2-1'} for n in ['a', 'b']]
        published = {'a': {'pkgname': 'a', 'pkgbase': 'a', '_path': str(old)}}
        original = Path.replace

        def replacement(path, target):
            if path.name == 'b-new.pkg.tar.zst':
                raise OSError('disk error')
            return original(path, target)

        with patch.object(Path, 'replace', replacement):
            with self.assertRaises(OSError):
                bot.promote(built, infos, published)
        self.assertEqual(old.read_text(), 'previous')
        self.assertEqual(old.with_name(old.name + '.sig').read_text(), 'signature')
        self.assertFalse((bot.PKGDEST / built[0].name).exists())
        self.assertEqual(set(published), {'a'})


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.logdir = Path(self.temp.name)
        self.capture = contextlib.redirect_stdout(io.StringIO())
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)
        self.runtime = Runtime(self.logdir)

    def test_stderr_cannot_contaminate_machine_readable_stdout(self):
        result = self.runtime.run([sys.executable, '-c', 'import sys; print("42"); print("warning", file=sys.stderr)'])
        self.assertEqual(result, '42')
        self.assertIn('warning', (self.logdir / 'buildbot.log').read_text())

    def test_invalid_utf8_output_does_not_crash(self):
        result = self.runtime.run([sys.executable, '-c', 'import os; os.write(1, b"a\\xffb")'])
        self.assertEqual(result, 'a\ufffdb')

    def test_failure_includes_exit_code_and_full_output_is_saved(self):
        with self.assertRaisesRegex(CommandError, 'exited 7'):
            self.runtime.run([sys.executable, '-c', 'print("diagnostic"); exit(7)'])
        self.assertIn('diagnostic', (self.logdir / 'buildbot.log').read_text())

    def test_build_output_need_not_be_captured(self):
        self.assertEqual(self.runtime.run([sys.executable, '-c', 'print("large build")'], capture=False), '')
        self.assertIn('large build', (self.logdir / 'buildbot.log').read_text())

    def test_timeout_terminates_process(self):
        with self.assertRaisesRegex(CommandError, 'timed out'):
            self.runtime.run([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=0.1)
        self.assertIn('command-aborted', (self.logdir / 'buildbot.log').read_text())

    def test_timeout_kills_children_that_ignore_sigterm(self):
        import time
        marker = self.logdir / 'child-survived'
        child = f'import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(0.7); Path({str(marker)!r}).touch()'
        parent = f'import subprocess,sys,time; subprocess.Popen([sys.executable, "-c", {child!r}]); time.sleep(30)'
        with self.assertRaises(CommandError):
            self.runtime.run([sys.executable, '-c', parent], timeout=0.3)
        time.sleep(0.7)
        self.assertFalse(marker.exists())

    def test_secrets_are_redacted_from_all_logs(self):
        secret = 'token-"quoted"-secret'
        with patch.dict(os.environ, {'GH_TOKEN': secret}):
            runtime = Runtime(self.logdir)
            with runtime.context('package'):
                runtime.run([sys.executable, '-c', 'import os; print(os.environ["GH_TOKEN"])'])
        for path in self.logdir.iterdir():
            self.assertNotIn(secret, path.read_text())
            self.assertIn('[REDACTED]', path.read_text())

    def test_multiline_secrets_and_nested_fields_are_redacted(self):
        secret = 'private-key-line-one\nprivate-key-line-two'
        with patch.dict(os.environ, {'GPG_PRIVATE_KEY': secret}):
            runtime = Runtime(self.logdir)
            runtime.run([sys.executable, '-c', 'import os; print(os.environ["GPG_PRIVATE_KEY"])'])
            runtime.log('nested', details={'secret': [secret]})
        for path in self.logdir.iterdir():
            self.assertNotIn('private-key-line-one', path.read_text())
            self.assertNotIn('private-key-line-two', path.read_text())

    def test_package_log_is_append_only(self):
        with self.runtime.context('package'):
            self.runtime.log('first')
            self.runtime.log('second')
        text = (self.logdir / 'package.log').read_text()
        self.assertIn('first', text)
        self.assertIn('second', text)

    def test_json_lines_are_parseable(self):
        self.runtime.log('fixture', text='line\nwith"quote')
        records = [json.loads(line) for line in (self.logdir / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(records[0]['text'], 'line\nwith"quote')


if __name__ == '__main__':
    unittest.main()
