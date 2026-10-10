import unittest
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import buildbot
from buildbot_abi import changed_vtables, runtime_paths, version_nodes, vtable_slots


class ArchiveScanTests(unittest.TestCase):
    def test_vtable_relocation_changes_preserve_slot_offsets(self):
        symbols = "00001000 00000028 D _ZTVExample"
        old = vtable_slots(symbols, """00001010 0001 R_X86_64_64 0000 eventFilter + 0
00001018 0002 R_X86_64_64 0000 timerEvent + 0""")
        new = vtable_slots(symbols, """00001010 0001 R_X86_64_RELATIVE 1234
00001018 0002 R_X86_64_64 0000 timerEvent + 0""")
        self.assertEqual(changed_vtables(old, new, {"eventFilter", "timerEvent"}), [])
        replaced = vtable_slots(symbols, """00001010 0001 R_X86_64_64 0000 other + 0
00001018 0002 R_X86_64_64 0000 timerEvent + 0""")
        self.assertEqual(changed_vtables(old, replaced, {"timerEvent"}), [])
        self.assertEqual(changed_vtables(old, replaced, {"eventFilter"}), ["_ZTVExample"])
        shortened = vtable_slots("00001000 00000018 D _ZTVExample", "")
        self.assertEqual(changed_vtables(old, shortened, {"timerEvent"}), ["_ZTVExample"])

    def test_abi_gate_builds_dynamic_versions_and_checks_the_result(self):
        cases = [(True, False, "2-1", "built"), (True, True, "2-1", "built"),
                 (True, False, "1-1", "needs-pkgrel"), (False, False, "2-1", "needs-pkgrel")]
        for dynamic, pending, built_version, expected in cases:
            with self.subTest(dynamic=dynamic, pending=pending, built_version=built_version), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                work, destination, logs = root / "work", root / "repo", root / "logs"
                for directory in (work, destination, logs, work / "example"):
                    directory.mkdir(parents=True, exist_ok=True)
                row = {"directory": "example", "url": "https://gitlab.com/LunaOS/example.git",
                       "ref": "HEAD", "makepkg_flags": "-s", "before": "", "after": ""}
                info = {"names": ["example"], "version": ".-1", "dynamic_version": dynamic,
                        "tree": "tree", "architectures": ["x86_64"], "nvchecker_revision": "abc"}
                previous = {"example": {"metadata": info.copy()}}
                if pending:
                    previous["example"].update(abi_pending=True, pending_source="abc", status="needs-pkgrel")
                triggers = {} if pending else {"example": {"published_version": "1-1", "versions": {}, "reason": "ABI changed"}}
                published = {"example": {"pkgver": "1-1", "_path": str(destination / "old.pkg.tar.zst")}}

                def run(args, **kwargs):
                    if args[0] == "vercmp":
                        return "0" if args[1] == args[2] else "-1" if args[1] == ".-1" else "1"
                    if "makepkg" in args:
                        Path(kwargs["env"]["PKGDEST"], "new.pkg.tar.zst").touch()
                    return ""

                mocks = {
                    "RUNTIME": MagicMock(), "WORK": work, "PKGDEST": destination, "LOGDIR": logs,
                    "STATE_PATH": root / "state.json", "read_manifest": MagicMock(return_value={"example": row}),
                    "load_state": MagicMock(return_value=({"example": "abc"}, previous)),
                    "source_sha": MagicMock(return_value="abc"), "published_packages": MagicMock(return_value=published),
                    "clone": MagicMock(return_value=(work / "example", "abc")),
                    "package_info": MagicMock(return_value=info), "check_arch_updates": MagicMock(return_value=({}, {})),
                    "check_nvchecker_updates": MagicMock(return_value=({}, {})), "cleanup_issues": MagicMock(),
                    "graph": MagicMock(return_value=({"example": set()}, {"example": set()})),
                    "available_versions": MagicMock(return_value={}), "elf_rebuild_triggers": MagicMock(return_value=triggers),
                    "create_pkgrel_issue": MagicMock(return_value=45), "run": MagicMock(side_effect=run),
                    "build_environment": MagicMock(return_value={}), "installed_versions": MagicMock(return_value={}),
                    "dependency_versions": MagicMock(return_value={}),
                    "archive_info": MagicMock(return_value={"pkgname": "example", "pkgver": built_version}),
                    "promote": MagicMock(), "emit_summary": MagicMock(), "outputs": MagicMock(), "log": MagicMock(),
                }
                with ExitStack() as stack:
                    stack.enter_context(patch.dict(buildbot.os.environ, {"BUILDBOT_PACKAGES": "", "BUILDBOT_FULL": "false"}))
                    for name, value in mocks.items():
                        stack.enter_context(patch.object(buildbot, name, value))
                    self.assertEqual(buildbot.main(), 0)
                result = json.loads((logs / "results.json").read_text())["example"]
                self.assertEqual(result["status"], expected)
                builds = [call for call in mocks["run"].call_args_list if "makepkg" in call.args[0]]
                self.assertEqual(len(builds), int(dynamic))
                self.assertEqual(mocks["promote"].call_count, int(expected == "built"))
                if expected == "built":
                    mocks["create_pkgrel_issue"].assert_not_called()

    def test_old_provider_uses_matching_lunaos_release(self):
        provider = "gnome-shell-extension-appindicator"
        consumer = {"_path": "consumer.pkg.tar.zst", "pkgver": "1-1",
                    "depends": [provider], "installed": [f"{provider}-66-1-any"]}
        packages = {"consumer": {"directory": "consumer", "url": "https://gitlab.com/LunaOS/consumer.git"}}
        elf = {"needed": {"libold.so"}, "provided": {"libold.so"}, "imports": set(),
               "exports_by_soname": {}, "runtime_dirs": {}}
        for published_version in ("66-1", "65-1"):
            with self.subTest(published_version=published_version):
                published = {"consumer": consumer, provider: {"pkgver": published_version, "_path": "lunaos.pkg.tar.zst"}}
                with (patch.object(buildbot, "arch_version", side_effect=lambda name: "1:66-1" if name == provider else None),
                      patch.object(buildbot, "archive_elf", return_value=elf) as scan,
                      patch.object(buildbot, "arch_archive", return_value="arch-old.pkg.tar.zst") as old_download,
                      patch.object(buildbot, "current_arch_archive", return_value="arch-new.pkg.tar.zst")):
                    # The installed old version differs from the current Arch epoch.
                    def vercmp(args, **kwargs):
                        return "0" if args[1:] == [published_version, "66-1"] and published_version == "66-1" else "-1"
                    with patch.object(buildbot, "run", side_effect=vercmp):
                        buildbot.elf_rebuild_triggers(packages, published, {}, {}, {}, {})
                old_path = "lunaos.pkg.tar.zst" if published_version == "66-1" else "arch-old.pkg.tar.zst"
                self.assertEqual(scan.call_args_list[1].args[0], old_path)
                self.assertEqual(old_download.call_count, int(published_version != "66-1"))

    def test_paths_do_not_include_verbose_dates(self):
        entries = [
            ("usr/bin/tool with spaces", "-rwxr-xr-x"),
            ("usr/bin/script", "-rwxr-xr-x"),
            ("usr/bin/static-pie", "-rwxr-xr-x"),
            ("usr/lib/libexample.so.1", "-rw-r--r--"),
            ("usr/lib/libexample.so", "lrwxrwxrwx"),
            ("usr/lib/python3.13/module.py", "-rw-r--r--"),
            ("usr/share/data", "-rw-r--r--"),
            ("../escape", "-rwxr-xr-x"),
            ("/absolute", "-rwxr-xr-x"),
        ]
        for date in ("Sep 12  2025", "Sep 13 04:10", "2025-09-12 04:10"):
            with self.subTest(date=date):
                extracted = []

                def run(args, **kwargs):
                    if args[:2] == ["bsdtar", "-tf"]:
                        return "\n".join(name for name, mode in entries)
                    if args[:2] == ["bsdtar", "-tvf"]:
                        return "\n".join(f"{mode} 0 root root 123 {date} {name}"
                                         for name, mode in entries)
                    if args[:2] == ["bsdtar", "-xf"]:
                        members = args[args.index("--") + 1:]
                        extracted.extend(members)
                        for name in members:
                            file = Path(args[args.index("-C") + 1]) / name
                            file.parent.mkdir(parents=True, exist_ok=True)
                            file.write_bytes(b"#!/bin/sh\n" if name.endswith("/script") else b"\x7fELF")
                    elif args[:2] == ["readelf", "-h"]:
                        self.assertFalse(args[-1].endswith("/script"))
                        return "Class: ELF64\nMachine: Advanced Micro Devices X86-64\nOS/ABI: UNIX - System V"
                    elif args[:2] == ["readelf", "-dW"]:
                        if args[-1].endswith("/static-pie"):
                            return "(SYMTAB) 0x360\n(STRSZ) 1 (bytes)"
                        return "(SYMTAB) 0x123\n(NEEDED) [libc.so.6]\n(SONAME) [libexample.so.1]"
                    elif args[:3] == ["nm", "-D", "--undefined-only"]:
                        self.assertFalse(args[-1].endswith("/static-pie"))
                        return " U puts@GLIBC_2.2.5"
                    elif args[:4] == ["nm", "-D", "-S", "--defined-only"]:
                        return "00000000 T example"
                    return ""

                with patch.object(buildbot, "run", side_effect=run):
                    result = buildbot.archive_elf(Path("example.pkg.tar.zst"), provider=True)
                self.assertEqual(extracted, ["usr/bin/script", "usr/bin/static-pie", "usr/bin/tool with spaces", "usr/lib/libexample.so.1"])
                self.assertEqual(result["needed"], {"libc.so.6"})
                self.assertEqual(result["runtime_dirs"], {"python": {"3.13"}})
                self.assertEqual(result["exports_by_soname"], {"libexample.so.1": {"example"}})

    def test_current_download_skips_all_dependencies(self):
        url = "https://mirror.example/mesa-26.2.3-2-x86_64.pkg.tar.zst"
        destination = Path("new.pkg.tar.zst")

        def run(args, **kwargs):
            if args[0] == "pacman":
                return url if args.count("--nodeps") == 2 else url + "\nhttps://mirror.example/dependency.pkg.tar.zst"
            self.assertEqual(args[-3:], ["--output", str(destination), url])
            return ""

        with (patch.object(buildbot, "run", side_effect=run),
              patch.object(buildbot, "archive_info", return_value={"pkgname": "mesa", "pkgver": "1:26.2.3-2"}),
              patch.object(buildbot, "arch_version", return_value="1:26.2.3-2")):
            self.assertEqual(buildbot.current_arch_archive("mesa", destination), destination)

    def test_abi_breaks_produce_issue_reasons(self):
        empty = {"needed": set(), "provided": set(), "imports": set(), "exports_by_soname": {},
                 "runtime_dirs": {}, "needed_versions": {}, "defined_versions": {}, "vtables": {}}
        cases = [
            ({"needed": {"libexample.so.1"}}, {"provided": {"libexample.so.1"}}, {}, "missing SONAMEs"),
            ({"needed": {"libexample.so.1"}, "imports": {"example@V1"}},
             {"provided": {"libexample.so.1"}, "exports_by_soname": {"libexample.so.1": {"example@@V1"}}},
             {"provided": {"libexample.so.1"}}, "missing symbols"),
            ({"needed": {"libexample.so.1"}, "needed_versions": {"libexample.so.1": {"V1"}}},
             {"defined_versions": {"libexample.so.1": {"V1"}}},
             {"defined_versions": {"libexample.so.1": {"V2"}}}, "missing version nodes"),
            ({"needed": {"libexample.so.1"}, "imports": {"method"}},
             {"vtables": {"_ZTVExample": ["method", "other"]}},
             {"vtables": {"_ZTVExample": ["other", "method"]}}, "vtable layout changed"),
            ({"needed": {"libexample.so.1"}, "imports": {"method"}},
             {"vtables": {"_ZTVExample": ["method"]}},
             {"vtables": {"_ZTVExample": ["method", "other"]}}, None),
            ({"runtime_dirs": {"python": {"3.12"}}}, {}, {}, "python files remain"),
        ]
        packages = {"consumer": {"directory": "consumer", "url": "https://gitlab.com/LunaOS/consumer.git"}}
        published = {"consumer": {"_path": "consumer.pkg.tar.zst", "pkgver": "1-1",
                                  "depends": ["example"], "installed": ["example-1-1-x86_64"]},
                     "example": {"_path": "new-lunaos.pkg.tar.zst", "pkgver": "2-1"}}
        for consumer, old, new, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                with (patch.object(buildbot, "arch_version", return_value=None),
                      patch.object(buildbot, "run", side_effect=lambda args, **kw: "0" if args[1] == args[2] else "-1"),
                      patch.object(buildbot, "archive_elf", side_effect=[empty | consumer, empty | old, empty | new]),
                      patch.object(buildbot, "arch_archive", return_value="old-arch.pkg.tar.zst"),
                      patch.object(buildbot, "current_arch_archive") as download):
                    triggers = buildbot.elf_rebuild_triggers(packages, published, {"example": "2-1", "python": "3.13.1-1"}, {}, {}, {})
                download.assert_not_called()  # Current local provider is reused.
                if expected_reason:
                    self.assertIn(expected_reason, triggers["consumer"]["reason"])
                    self.assertEqual(triggers["consumer"]["published_version"], "1-1")
                else:
                    self.assertEqual(triggers, {})

    def test_version_nodes_vtables_and_runtime_parsers(self):
        defined, needed = version_nodes("""Version definition section '.gnu.version_d':
          Flags: BASE Index: 1 Name: libexample.so
          Flags: none Index: 2 Name: V1
        Version needs section '.gnu.version_r':
          Version: 1 File: libc.so.6 Cnt: 1
          Name: GLIBC_2.2.5 Flags: none Version: 3
        """)
        self.assertEqual(defined, {"V1"})
        self.assertEqual(needed, {"libc.so.6": {"GLIBC_2.2.5"}})
        tables = vtable_slots("00001000 00000018 D _ZTVExample", """00001008 0001 R_X86_64_64 0000 _ZTIExample + 0
00001010 0002 R_X86_64_64 0000 method + 0""")
        self.assertEqual(tables, {"_ZTVExample": [None, None, "method"]})
        self.assertEqual(changed_vtables(tables, {}, {"method"}), ["_ZTVExample"])
        self.assertEqual(changed_vtables(tables, {}, {"unrelated"}), [])
        self.assertEqual(changed_vtables({"t": ["__cxa_pure_virtual"]}, {}, {"__cxa_pure_virtual"}), [])
        self.assertEqual(runtime_paths(["usr/lib/python3.12/module.py", "usr/lib/perl5/vendor_perl/5.40/a.pm",
                                        "usr/lib/ruby/gems/3.3.0/a.so", "usr/lib/ghc-9.6.6/a.so",
                                        "usr/lib/python3.11/"]),
                         {"python": {"3.12"}, "perl": {"5.40"}, "ruby": {"3.3"}, "ghc": {"9.6.6"}})
        self.assertEqual(runtime_paths(["usr/share/plugin.cpython-314-x86_64-linux-gnu.so", "usr/share/plugin.abi3.so"]),
                         {"python": {"3.14"}})

    def test_aur_python_detection_and_issue_lifecycle(self):
        row = {"directory": "portprotonqt", "url": "https://github.com/archlinux/aur.git", "ref": "portprotonqt"}
        archive = {"pkgver": "1.4.1-1", "_path": "portprotonqt.pkg.tar.zst", "depends": ["python"]}
        elf = {"needed": set(), "provided": set(), "imports": set(), "runtime_dirs": {"python": {"3.14"}}}
        with (patch.object(buildbot, "archive_elf", return_value=elf), patch.object(buildbot, "arch_version", return_value=None)):
            triggers = buildbot.elf_rebuild_triggers({"portprotonqt": row}, {"portprotonqt": archive},
                                                   {"python": "3.15.0-1"}, {}, {}, {})
            self.assertIn("python files remain under 3.14", triggers["portprotonqt"]["reason"])
            self.assertEqual(buildbot.elf_rebuild_triggers({"portprotonqt": row}, {"portprotonqt": archive},
                                                        {"python": "3.14.8-1"}, {}, {}, {}), {})
        with tempfile.TemporaryDirectory() as temp:
            with (patch.object(buildbot, "LOGDIR", Path(temp)),
                  patch.object(buildbot, "run", side_effect=['[]', 'https://github.com/Lenuma-inc/lunaos-repo/issues/123'])):
                number = buildbot.create_pkgrel_issue("portprotonqt", row, "abc123", "1.4.1-1", "1.4.1-1",
                                                     {}, triggers["portprotonqt"]["reason"])
            self.assertEqual(number, 123)
            body = (Path(temp) / "portprotonqt-issue.md").read_text()
        issue = {"number": 123, "title": "[rebuild] portprotonqt: bump pkgrel", "body": body}
        for current in ("1.4.1-1", "1.4.1-2"):
            with self.subTest(current=current):
                def run(args, **kwargs):
                    if args[:3] == ["gh", "issue", "list"]:
                        return json.dumps([issue])
                    return "0" if args[0] == "vercmp" and args[1] == args[2] else "1"
                with (patch.object(buildbot, "run", side_effect=run) as command,
                      patch.object(buildbot, "published_source_matches", return_value=True),
                      patch.object(buildbot, "log")):
                    buildbot.cleanup_issues({"portprotonqt": row}, {"portprotonqt": archive | {"pkgver": current}}, {}, set(), {})
                closes = [call for call in command.call_args_list if call.args[0][:3] == ["gh", "issue", "close"]]
                self.assertEqual(len(closes), int(current == "1.4.1-2"))


if __name__ == "__main__":
    unittest.main()
