# Automatic ABI checks

The buildbot scans published packages listed in `packages.tsv`, including AUR sources, on every scheduled run. No per-package trigger list is required. It implements the useful ELF checks from [Chaotic's backend](https://github.com/chaotic-cx/chaotic-next/tree/main/backend/src/repo-manager) in the existing Python buildbot, without Redis, a database, or another service.

## What it checks

- SONAMEs removed by an updated provider.
- Required dynamic symbols and ELF version nodes removed by an updated provider, even with an unchanged SONAME.
- Reordered or removed C++ vtable slots referenced by the consumer. Appending slots is compatible. This is a relocation/import heuristic, not a complete ABI check; class size and field-layout changes are not detected.
- Files in obsolete Python, Perl, Ruby, and GHC runtime directories, and CPython-specific extension filenames outside those directories.

The old dependency version comes from `.BUILDINFO`, with cached dependency versions as a fallback. An exact-version provider archive from the downloaded LunaOS release is reused when available; otherwise the bot fetches the old version from Arch Archive. Current versions follow pacman repository priority. A matching current LunaOS archive is reused; other current packages are downloaded through pacman's URL lookup. Library ownership under `/usr/lib` also identifies indirect providers not named in `.PKGINFO`.

## Python packages in LunaOS

The checked release contains `bauh`, `envycontrol`, and `portprotonqt` modules under `usr/lib/python3.14/site-packages`. Moving to Python 3.15 requires rebuilding them, including pure Python packages with `arch=any`. Python 3.14 patch updates do not trigger this check. CPython extensions such as `module.cpython-314-x86_64-linux-gnu.so` also carry a minor-version requirement; `.abi3.so` alone does not.

Nautilus extensions under `usr/share/nautilus-python/extensions` and the standalone `pylinuxwheel` script have no version-bound installation path, so a Python update alone does not mark them broken. The scan does not execute applications or check Python API/import compatibility.

## What happens after detection

The bot opens an issue in this repository with the reason and versions, marks the package `needs-pkgrel`, and waits for a source update. AUR packages receive ABI issues here too; the bot does not submit requests to AUR. It does not modify or push source repositories or automatically increase `pkgrel`.

When source metadata reports a version newer than every published split-package version, normal building and publishing resumes. Duplicate requests for the same source revision are suppressed. ABI issues stay open until a newer archive is published; the old archive having the same source revision is not enough to resolve an ABI break.

## Requirements and verification

The workflow installs Python 3.11+, pacman/vercmp, curl, libarchive (`bsdtar`), and binutils (`readelf`, `nm`). It supplies synchronized package databases and GitHub credentials for issues, and downloads the release into `PKGDEST` before running the bot. No additional workflow switch is required.

The unit tests need only Python and make no network requests:

```sh
python3 -m unittest -v test_buildbot_abi
```

Full integration needs the package tools and archives; the ELF scanner also runs on non-Arch Linux when equivalent tools are available. It never executes binaries from scanned archives.

Only Linux x86-64 ELF64 objects participate in ABI comparisons. Scripts, empty files, static ELF objects, and foreign binaries are skipped. Old LunaOS/Chaotic versions absent from both the downloaded release and Arch Archive cannot be compared. Missing build provenance and unresolved indirect providers limit coverage. Scan failures are logged as `abi-provider-scan-failed` / `abi-consumer-scan-failed` rather than treated as evidence of compatibility. Runtime-path checks do not need old provider archives.

Archives are unpacked in temporary directories, with a 5,000-candidate limit per archive. Provider results are shared within a run; there is no persistent ELF index.
