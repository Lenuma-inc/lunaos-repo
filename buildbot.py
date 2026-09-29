#!/usr/bin/env python3
"""Incremental Arch package builder with dependency-aware scheduling."""

import csv
import datetime as dt
from functools import cmp_to_key
import json
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
import tomllib
from urllib.parse import quote, unquote, urlsplit
from pathlib import Path

from buildbot_runtime import Runtime, atomic_json
from buildbot_abi import RUNTIME_PATHS, changed_vtables, runtime_paths, version_nodes, vtable_slots

ROOT = Path(__file__).resolve().parent
WORK = Path(os.environ.get("BUILDBOT_WORK", "/tmp/lunaos-buildbot"))
PKGDEST = Path(os.environ.get("PKGDEST", "/tmp/lunaos-repo"))
LOGDIR = Path(os.environ.get("BUILDBOT_LOGDIR", "/tmp/lunaos-build-logs"))
STATE_PATH = Path(os.environ.get("BUILDBOT_STATE", ROOT / ".build-state" / "state.json"))
RUNTIME = None
FAILURES = {"failed", "blocked", "source-failed", "source-check-failed"}
ARCH_REPOSITORY_PACKAGES = None


def log(event, **fields):
    RUNTIME.log(event, **fields)


def run(args, **kwargs):
    kwargs.setdefault("timeout", int(os.environ.get("BUILDBOT_COMMAND_TIMEOUT", "600")))
    return RUNTIME.run(args, **kwargs)


def network_run(args, **kwargs):
    for attempt in range(1, 4):
        try:
            return run(args, **kwargs)
        except (RuntimeError, OSError) as exc:
            log("network-failed", attempt=attempt, error=str(exc))
            if attempt == 3:
                raise
            time.sleep(attempt * 2)


def read_manifest():
    with (ROOT / "packages.tsv").open(newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        if reader.fieldnames != ["directory", "url", "ref", "makepkg_flags", "before", "after"]:
            raise RuntimeError("packages.tsv has an invalid header")
        rows = list(reader)
    if not rows or len({row["directory"] for row in rows}) != len(rows):
        raise RuntimeError("packages.tsv is empty or contains duplicate directories")
    for row in rows:
        if None in row or any(value is None for value in row.values()):
            raise RuntimeError(f"invalid manifest row: {row}")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9@._+-]*", row["directory"]) or row["directory"] == "packages":
            raise RuntimeError(f"unsafe package directory: {row['directory']}")
        if not row["url"].startswith("https://") or not row["ref"] or row["ref"].startswith("-"):
            raise RuntimeError(f"invalid source for {row['directory']}")
        shlex.split(row["makepkg_flags"])
    return {row["directory"]: row for row in rows}


def source_sha(row):
    ref = "HEAD" if row["ref"] == "HEAD" else f"refs/heads/{row['ref']}"
    output = network_run(["git", "ls-remote", "--symref", row["url"], ref])
    target = "HEAD" if row["ref"] == "HEAD" else ref
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[-1] == target and fields[0] != "ref:":
            return fields[0]
    raise RuntimeError(f"remote ref not found: {row['url']} {row['ref']}")


def clone(row, expected=None):
    path = WORK / row["directory"]
    if path.exists():
        shutil.rmtree(path)
    args = ["git", "clone", "--depth", "1"]
    if row["ref"] != "HEAD":
        args += ["--branch", row["ref"], "--single-branch"]
    args += [row["url"], str(path)]
    # Failed shallow clones can leave a directory behind; remove it per attempt.
    for attempt in range(1, 4):
        if path.exists():
            shutil.rmtree(path)
        try:
            run(args)
            break
        except (RuntimeError, OSError):
            if attempt == 3:
                raise
            log("clone-retry", attempt=attempt)
            time.sleep(attempt * 2)
    revision = run(["git", "rev-parse", "HEAD"], cwd=path)
    if expected and revision != expected:
        network_run(["git", "fetch", "--depth", "1", "origin", expected], cwd=path)
        run(["git", "checkout", "--detach", expected], cwd=path)
        revision = run(["git", "rev-parse", "HEAD"], cwd=path)
    run(["chown", "-R", "user:user", str(path)])
    return path, revision


def build_inputs_changed(path, previous, current):
    """Ignore nvchecker-only commits when deciding whether a pkgrel was missed."""
    run(["runuser", "-u", "user", "--", "git", "fetch", "--depth", "1", "origin", previous], cwd=path)
    files = run(["runuser", "-u", "user", "--", "git", "diff", "--name-only", previous, current], cwd=path).splitlines()
    return any(Path(name).name != ".nvchecker.toml" for name in files)


def srcinfo(path):
    file = path / ".SRCINFO"
    if (path / "PKGBUILD").is_file():
        # A stale committed .SRCINFO must not hide a new PKGBUILD version.
        text = run(["runuser", "-u", "user", "--", "makepkg", "--printsrcinfo"], cwd=path, env=build_environment())
    else:
        text = file.read_text()
    deps, build_deps, provides, names, architectures = set(), set(), set(), set(), set()
    version = {}
    pkgbase = None
    for line in text.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if not sep:
            continue
        if key == "pkgbase":
            pkgbase = value
        elif key == "pkgname":
            names.add(value)
        elif key in {"epoch", "pkgver", "pkgrel"}:
            version[key] = value
        elif key == "arch":
            architectures.add(value)
        elif key in {"depends", "makedepends", "checkdepends", "depends_x86_64", "makedepends_x86_64", "checkdepends_x86_64"}:
            deps.add(re.split(r"[<>=]", value, maxsplit=1)[0])
            if key.startswith(("makedepends", "checkdepends")):
                build_deps.add(re.split(r"[<>=]", value, maxsplit=1)[0])
        elif key in {"provides", "provides_x86_64"}:
            provides.add(re.split(r"[<>=]", value, maxsplit=1)[0])
    if not names:
        raise RuntimeError(f"{path}/.SRCINFO contains no package names")
    info = {"deps": sorted(deps), "provides": sorted(provides), "names": sorted(names),
            "build_deps": sorted(build_deps), "architectures": sorted(architectures)}
    if pkgbase:
        info["pkgbase"] = pkgbase
    if version.get("pkgver") and version.get("pkgrel"):
        epoch = version.get("epoch", "0")
        info["version"] = (f"{epoch}:" if epoch != "0" else "") + version["pkgver"] + "-" + version["pkgrel"]
    pkgbuild = (path / "PKGBUILD").read_text() if (path / "PKGBUILD").exists() else ""
    nvchecker = path / ".nvchecker.toml"
    if nvchecker.is_file():
        info["nvchecker_config"] = nvchecker.read_text()
    info["dynamic_version"] = bool(re.search(r"(?m)^\s*(?:function\s+)?pkgver\s*\(\)", pkgbuild))
    if (path / ".git").exists():
        info["source_timestamp"] = int(run(["runuser", "-u", "user", "--", "git", "show", "-s", "--format=%ct", "HEAD"], cwd=path))
    return info


def package_info(path):
    try:
        info = srcinfo(path)
        info["tree"] = run(["runuser", "-u", "user", "--", "git", "rev-parse", "HEAD^{tree}"], cwd=path)
        return info
    except Exception as exc:
        raise RuntimeError(f"cannot read .SRCINFO: {exc}") from exc


def nvchecker_config_url(row, revision):
    if not lunaos_source(row):
        return None
    source = urlsplit(row["url"].removesuffix(".git"))
    path = source.path.strip("/")
    if source.hostname == "gitlab.com":
        return f"https://gitlab.com/{path}/-/raw/{quote(revision, safe='')}/.nvchecker.toml"
    if source.hostname == "github.com" and len(path.split("/")) == 2:
        return f"https://raw.githubusercontent.com/{path}/{revision}/.nvchecker.toml"
    return None


def fetch_nvchecker_config(row, revision):
    url = nvchecker_config_url(row, revision)
    if not url:
        return ""
    for attempt in range(1, 4):
        try:
            output = run(["curl", "--silent", "--show-error", "--location", "--max-time", "30",
                          "--write-out", "\\n%{http_code}", url], check=False, timeout=40)
            body, _, status = output.rpartition("\n")
            if status == "200":
                return body
            if status == "404":
                return ""
            raise RuntimeError(f"HTTP {status or 'request failed'} for nvchecker config")
        except Exception as exc:
            if attempt == 3:
                raise
            log("nvchecker-config-retry", package=row["directory"], attempt=attempt, error=str(exc))
            time.sleep(attempt * 2)
    return ""


def graph(packages, metadata):
    names, provides = {}, {}
    for directory in packages:
        info = metadata.get(directory, {})
        for name in info.get("names", []):
            names.setdefault(name, set()).add(directory)
        for name in info.get("provides", []):
            provides.setdefault(name, set()).add(directory)
    dependencies = {directory: set() for directory in packages}
    dependents = {directory: set() for directory in packages}
    for directory in packages:
        info = metadata.get(directory, {})
        for name in info.get("deps", []):
            # The build image already supplies this group; depending on its meta-package can create bootstrap cycles.
            if name == "base-devel":
                continue
            providers = names.get(name) or provides.get(name, set())
            if len(providers) == 1 and (dependency := next(iter(providers))) != directory:
                dependencies[directory].add(dependency)
                dependents[dependency].add(directory)
            elif len(providers) > 1:
                log("ambiguous-provider", dependency=name, consumers=directory, providers=sorted(providers))
    return dependencies, dependents


def archive_info(path):
    info = run(["bsdtar", "-xOf", str(path), ".PKGINFO"])
    parsed = {key: value for line in info.splitlines() if (key := line.partition(" = ")[0]) and (value := line.partition(" = ")[2])}
    parsed["depends"] = [re.split(r"[<>=]", line.partition(" = ")[2], maxsplit=1)[0]
                         for line in info.splitlines() if line.startswith("depend = ")]
    if not all(parsed.get(key) for key in ("pkgname", "pkgver", "arch")):
        raise RuntimeError(f"invalid package archive: {path}")
    buildinfo = run(["bsdtar", "-xOf", str(path), ".BUILDINFO"], check=False)
    for line in buildinfo.splitlines():
        key, sep, value = line.partition(" = ")
        if not sep:
            continue
        if key == "builddate":
            parsed["builddate"] = value
        elif key == "installed":
            parsed.setdefault("installed", []).append(value)
    provenance_path = Path(str(path) + ".buildbot.json")
    if provenance_path.is_file():
        try:
            provenance = json.loads(provenance_path.read_text())
            if provenance.get("pkgname") == parsed["pkgname"] and provenance.get("pkgver") == parsed["pkgver"]:
                parsed["buildbot"] = provenance
        except (OSError, ValueError):
            pass
    return parsed


def installed_package_version(installed, name):
    for entry in installed:
        if not entry.startswith(name + "-"):
            continue
        version = entry[len(name) + 1:].rsplit("-", 2)
        if len(version) == 3 and version[2] in {"any", "x86_64"}:
            return f"{version[0]}-{version[1]}"
    return None


def archive_elf(path, provider=False):
    """Read linked sonames and dynamic symbols from host ELF files in a package."""
    names = run(["bsdtar", "-tf", str(path)]).splitlines()
    verbose = run(["bsdtar", "-tvf", str(path)]).splitlines()
    candidates = set()
    # Both listings follow archive order; verbose dates vary with age and locale.
    for name, line in zip(names, verbose, strict=True):
        mode = line.split(maxsplit=1)[0]
        if (mode.startswith("-") and not name.endswith("/")
                and ("x" in mode or re.search(r"\.so(?:\.[0-9]+)*$", name))):
            candidates.add(name)
    candidates = {name for name in candidates if not Path(name).is_absolute() and ".." not in Path(name).parts}
    if len(candidates) > 5000:
        raise RuntimeError(f"ELF scan candidate limit exceeded in {path}: {len(candidates)}")

    needed, provided, imports, exports_by_soname = set(), set(), set(), {}
    defined_versions, needed_versions, vtables = {}, {}, {}
    with tempfile.TemporaryDirectory(prefix="buildbot-elf-") as temp:
        for offset in range(0, len(candidates), 400):
            run(["bsdtar", "-xf", str(path), "-C", temp, "--", *sorted(candidates)[offset:offset + 400]])
        for name in sorted(candidates):
            file = Path(temp) / name
            with file.open("rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    continue
            header = run(["readelf", "-h", str(file)], check=False)
            if ("Class:" not in header or "ELF64" not in header or "Machine:" not in header or "X86-64" not in header
                    or not re.search(r"OS/ABI:\s+UNIX - (?:System V|GNU)", header)):
                continue
            dynamic = run(["readelf", "-dW", str(file)], check=False)
            if (not re.search(r"\(SYMTAB\)", dynamic) or
                    re.search(r"\(STRSZ\)\s+1\s+\(bytes\)", dynamic)):
                continue  # Static PIE can have SYMTAB with only the null symbol.
            needed.update(re.findall(r"\(NEEDED\).*\[([^]]+)\]", dynamic))
            soname = re.search(r"\(SONAME\).*\[([^]]+)\]", dynamic)
            if soname:
                provided.add(soname[1])
            defined, required_versions = version_nodes(run(["readelf", "-VW", str(file)], check=False))
            for library, versions in required_versions.items():
                needed_versions.setdefault(library, set()).update(versions)
            if soname:
                defined_versions.setdefault(soname[1], set()).update(defined)
            undefined = run(["nm", "-D", "--undefined-only", str(file)], check=False)
            for line in undefined.splitlines():
                fields = line.split()
                if len(fields) >= 2 and fields[-2] == "U":
                    imports.add(fields[-1])
            if provider and re.search(r"\.so(?:\.[0-9]+)*$", name):
                defined = run(["nm", "-D", "-S", "--defined-only", str(file)], check=False)
                if soname:
                    exports_by_soname.setdefault(soname[1], set()).update(
                        line.split()[-1] for line in defined.splitlines() if len(line.split()) >= 2)
                if "_ZTV" in defined:
                    vtables.update(vtable_slots(defined, run(["readelf", "-rW", str(file)], check=False)))
    return {"needed": needed, "provided": provided, "imports": imports,
            "exports_by_soname": exports_by_soname,
            "defined_versions": defined_versions, "needed_versions": needed_versions, "vtables": vtables,
            "runtime_dirs": runtime_paths(names)}


def arch_archive(package, version, destination):
    plain_version = version.split(":", 1)[-1]
    base = f"https://archive.archlinux.org/packages/{package[0]}/{package}/"
    listing = network_run(["curl", "--fail", "--location", "--silent", "--show-error", "--retry", "2", base])
    files = [unquote(name) for name in re.findall(r'href="([^\"]+\.pkg\.tar\.zst)"', listing)]
    matches = [name for name in files if name.startswith(f"{package}-{version}-")]
    if not matches and plain_version != version:
        matches = [name for name in files if name.startswith(f"{package}-{plain_version}-")]
    matches = [name for name in matches if name.endswith(("-x86_64.pkg.tar.zst", "-any.pkg.tar.zst"))]
    if len(matches) != 1:
        raise RuntimeError(f"expected one Arch Archive package for {package} {version}, found {matches}")
    url = base + quote(matches[0], safe="-._")
    network_run(["curl", "--fail", "--location", "--silent", "--show-error", "--retry", "2",
                 "--output", str(destination), url])
    info = archive_info(destination)
    if info["pkgname"] != package or info["pkgver"].split(":", 1)[-1] != plain_version:
        raise RuntimeError(f"Arch Archive returned unexpected package for {package} {version}")
    return destination


def current_arch_archive(package, destination, expected=None):
    output = run(["pacman", "-Sp", "--nodeps", "--nodeps", "--print-format", "%l", package])
    urls = [line.strip() for line in output.splitlines()
            if urlsplit(line.strip()).scheme in {"http", "https"}]
    if len(urls) != 1:
        raise RuntimeError(f"expected one current Arch download URL for {package}, found {len(urls)}")
    run(["curl", "--fail", "--location", "--silent", "--show-error", "--retry", "2",
         "--output", str(destination), urls[0]])
    info = archive_info(destination)
    expected = expected or arch_version(package)
    if (info["pkgname"] != package or
            (expected and info["pkgver"].split(":", 1)[-1] != expected.split(":", 1)[-1])):
        raise RuntimeError(f"Arch returned unexpected current package for {package}: {info['pkgver']} != {expected}")
    return destination


def arch_version(package):
    global ARCH_REPOSITORY_PACKAGES
    if ARCH_REPOSITORY_PACKAGES is None:
        ARCH_REPOSITORY_PACKAGES = {}
        for line in run(["pacman", "-Sl"]).splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[0] in {"core", "extra", "multilib"}:
                ARCH_REPOSITORY_PACKAGES.setdefault(fields[1], fields[2])
    return ARCH_REPOSITORY_PACKAGES.get(package)


def symbol_key(symbol):
    name, _, version = symbol.partition("@")
    return name, version.lstrip("@")


def elf_rebuild_triggers(packages, published, available, previous, refs, metadata):
    """Find published LunaOS packages that lost an ELF ABI or runtime path."""
    runtimes = {name: available.get(name) or arch_version(name) for name in RUNTIME_PATHS}
    triggers, provider_cache, version_cache = {}, {}, {}
    for package, archive in published.items():
        owner = packages.get(package) or packages.get(archive.get("pkgbase", ""))
        if not owner:
            continue
        directory = owner["directory"]
        old_state = previous.get(directory, {})
        if (old_state.get("abi_pending") and old_state.get("pending_source") == refs.get(directory)
                and not published_source_matches(metadata.get(directory, {}), published, refs.get(directory, ""))):
            continue
        try:
            consumer = archive_elf(archive["_path"])
        except Exception as exc:
            log("abi-consumer-scan-failed", package=package, error=str(exc))
            continue
        reasons, version_changes = [], {}
        for runtime, current in runtimes.items():
            old = (installed_package_version(archive.get("installed", []), runtime) or
                   old_state.get("dependency_versions", {}).get(runtime))
            components = 3 if runtime == "ghc" else 2
            match = re.match(r"(?:[0-9]+:)?(\d+(?:\.\d+){" + str(components - 1) + r"})", current or "")
            for version in sorted(consumer.get("runtime_dirs", {}).get(runtime, ())):
                if match and version != match[1]:
                    reasons.append(f"{runtime} files remain under {version} (current {runtime} is {match[1]})")
                    version_changes[runtime] = (old or version, current)
        provider_names = set(archive.get("depends", [])) if consumer["needed"] or consumer["imports"] else set()
        # Resolve linked libraries even when the provider is an indirect dependency.
        for soname in sorted(consumer["needed"]):
            if "/" not in soname and (library := Path("/usr/lib") / soname).is_file():
                provider_names.update(run(["pacman", "-Qqo", "--", str(library)], check=False).splitlines())
        changed_providers = []
        for dependency in sorted(provider_names):
            old_version = (installed_package_version(archive.get("installed", []), dependency) or
                           old_state.get("dependency_versions", {}).get(dependency))
            if dependency not in version_cache:
                version_cache[dependency] = available.get(dependency) or arch_version(dependency)
            new_version = version_cache[dependency]
            if not old_version or not new_version or int(run(["vercmp", old_version, new_version])) == 0:
                continue
            changed_providers.append((dependency, old_version, new_version))
        for dependency, old_version, new_version in changed_providers:
            key = (dependency, old_version, new_version)
            if key not in provider_cache:
                try:
                    with tempfile.TemporaryDirectory(prefix="buildbot-abi-provider-") as temp:
                        local = published.get(dependency)
                        if local and int(run(["vercmp", local["pkgver"], old_version])) == 0:
                            old_path = local["_path"]
                        else:
                            old_path = arch_archive(dependency, old_version, Path(temp) / "old.pkg.tar.zst")
                        if local and int(run(["vercmp", local["pkgver"], new_version])) == 0:
                            new_path = local["_path"]
                        else:
                            new_path = current_arch_archive(dependency, Path(temp) / "new.pkg.tar.zst", new_version)
                        provider_cache[key] = (archive_elf(old_path, provider=True), archive_elf(new_path, provider=True))
                except Exception as exc:
                    provider_cache[key] = exc
                    log("abi-provider-scan-failed", dependency=dependency,
                        old=old_version, current=new_version, error=str(exc))
            old_provider, new_provider = provider_cache[key] if not isinstance(provider_cache[key], Exception) else (None, None)
            if not old_provider:
                continue
            external_needed = consumer["needed"] - consumer["provided"]
            lost_sonames = external_needed & old_provider["provided"] - new_provider["provided"]
            required = {symbol_key(item) for item in consumer["imports"]}
            lost_symbols = set()
            for soname in external_needed & old_provider["provided"] & new_provider["provided"]:
                old_exports = {symbol_key(item) for item in old_provider["exports_by_soname"].get(soname, ())}
                new_exports = {symbol_key(item) for item in new_provider["exports_by_soname"].get(soname, ())}
                lost_symbols.update(required & (old_exports - new_exports))
            lost_symbols = sorted(lost_symbols)
            lost_versions = []
            for soname, required in consumer.get("needed_versions", {}).items():
                if soname in consumer["provided"]:
                    continue
                old_nodes = old_provider.get("defined_versions", {}).get(soname, set())
                new_nodes = new_provider.get("defined_versions", {}).get(soname, set())
                lost_versions.extend(f"{soname}:{node}" for node in sorted(required & (old_nodes - new_nodes)))
            vtables = changed_vtables(old_provider.get("vtables", {}), new_provider.get("vtables", {}), consumer["imports"])
            if lost_sonames or lost_symbols or lost_versions or vtables:
                detail = []
                if lost_sonames:
                    detail.append("missing SONAMEs " + ", ".join(sorted(lost_sonames)))
                if lost_symbols:
                    detail.append("missing symbols " + ", ".join(f"{name}@{version}" if version else name
                                                                      for name, version in lost_symbols[:8]))
                if lost_versions:
                    detail.append("missing version nodes " + ", ".join(lost_versions[:8]))
                if vtables:
                    detail.append("vtable layout changed " + ", ".join(vtables[:8]))
                reasons.append(f"{dependency} {old_version} → {new_version}: " + "; ".join(detail))
                version_changes[dependency] = (old_version, new_version)
        if reasons:
            trigger = triggers.setdefault(directory, {"versions": {}, "reasons": [],
                                                      "published_version": archive["pkgver"]})
            trigger["versions"].update(version_changes)
            trigger["reasons"].extend(reasons)
    for trigger in triggers.values():
        trigger["reason"] = "; ".join(dict.fromkeys(trigger.pop("reasons")))
    return triggers


def published_source_matches(info, published, revision):
    archives = [published.get(name) for name in info.get("names", [])]
    if not archives or any(archive is None for archive in archives):
        return False
    provenance = [archive.get("buildbot", {}) for archive in archives]
    if all(item.get("source_revision") for item in provenance):
        return all(item["source_revision"] == revision or
                   item.get("source_tree") == info.get("tree") for item in provenance)
    # ponytail: legacy packages lack source hashes; build date is a bootstrap heuristic, exact sidecars replace it.
    source_timestamp = info.get("source_timestamp")
    return bool(source_timestamp and all(
        archive.get("builddate") and int(archive["builddate"]) > source_timestamp for archive in archives))


def published_packages():
    result = {}
    for path in sorted(PKGDEST.glob("*.pkg.tar.zst")):
        if info := archive_info(path):
            if name := info.get("pkgname"):
                if name in result:
                    previous = result[name]
                    comparison = int(run(["vercmp", info["pkgver"], previous["pkgver"]]))
                    if comparison == 0:
                        raise RuntimeError(f"duplicate published archives for {name} at {info['pkgver']}: "
                                           f"{previous['_path']}, {path}")
                    stale_info = previous if comparison > 0 else info
                    stale = Path(stale_info["_path"]) if comparison > 0 else path
                    current = info if comparison > 0 else previous
                    stale.unlink()
                    log("stale-published-archive-pruned", package=name, stale_version=stale_info["pkgver"],
                        path=str(stale), kept=current["pkgver"])
                    if comparison < 0:
                        continue
                info["_path"] = str(path)
                result[name] = info
    return result


def lunaos_source(row):
    return row["url"].startswith("https://gitlab.com/LunaOS/")


def create_pkgrel_issue(name, row, revision, current, published, dependency_changes, reason=None):
    if not lunaos_source(row) and not reason:
        raise ValueError(f"cannot request a pkgrel change in unmaintained source: {row['url']}")
    title = f"[rebuild] {name}: bump pkgrel"
    marker = f"<!-- lunaos-buildbot:rebuild:{name}:{revision}:{current}:{published} -->"
    issues = json.loads(run(["gh", "issue", "list", "--state", "all", "--search", f'"{title}" in:title',
                             "--limit", "1000", "--json", "number,title,state,body"]))
    existing = next((issue for issue in issues if issue["title"] == title and
                     (issue.get("state", "OPEN") == "OPEN" or marker in issue.get("body", "") or
                      f"Source revision: `{revision}`" in issue.get("body", ""))), None)
    if existing:
        return existing["number"]
    changed = ", ".join(f"`{dep}` {old} → {new}" for dep, (old, new) in dependency_changes.items()) or "package source changed"
    source = row["url"].removesuffix(".git")
    source_link = f"{source}/-/tree/{revision}" if "gitlab" in source else f"{source}/tree/{revision}"
    if reason:
        explanation = (f"The Buildbot ABI/runtime scan found that `{name}` needs a rebuild: {reason}. "
                       f"LunaOS publishes `{published}`; Buildbot checked source revision `{revision}`. "
                       "Update or rebuild this package source and increase `pkgrel` when needed.")
    elif dependency_changes:
        explanation = (f"A declared dependency of `{name}` changed ({changed}), but the source still produces "
                       f"`{current}`, the same version LunaOS publishes. Rebuild the package and bump `pkgrel` "
                       "in its source so the new archive can upgrade the published package.")
    else:
        explanation = (f"A source change for `{name}` still produces `{current}`, the same version LunaOS publishes. "
                       "Bump `pkgrel` (or update `pkgver` if needed) in its source.")
    body = (f"{explanation}\n\n[Package source]({source_link})\n\nTrigger: {changed}"
            f"\n\nSource revision: `{revision}`\n\n{marker}")
    if reason:
        body += "\n<!-- lunaos-buildbot:abi -->"
    body_path = LOGDIR / f"{name}-issue.md"
    body_path.write_text(body)
    url = run(["gh", "issue", "create", "--title", title, "--body-file", str(body_path)])
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def upstream_version(version):
    # Arch/LunaOS pkgrel and epoch are packaging decisions, not upstream releases.
    return version.rsplit(":", 1)[-1].rsplit("-", 1)[0]


def check_arch_updates(packages, published, metadata):
    try:
        issues = json.loads(run(["gh", "issue", "list", "--state", "all", "--limit", "1000", "--json", "number,title,state,body"]))
    except Exception as exc:
        log("arch-issues-unavailable", error=str(exc))
        return set(), {}
    checked = set()
    updates = set()
    arch_versions = {}
    for package_name, current in published.items():
        owner = packages.get(package_name) or packages.get(current.get("pkgbase", ""))
        if not owner:
            continue
        # AUR mirrors are not LunaOS-maintained forks, and split outputs share one issue.
        if not owner["url"].startswith("https://gitlab.com/LunaOS/") or owner["directory"] in checked:
            continue
        try:
            name = owner["directory"]
            title = f"[update] {name}: sync Arch package"
            existing = next((issue for issue in issues if issue["title"] == title and issue.get("state", "OPEN") == "OPEN"), None)
            arch_version = arch_repo = arch_arch = ""
            for repo in ("core", "extra", "multilib"):
                result = run(["pacman", "-Si", f"{repo}/{package_name}"], check=False)
                version = re.search(r"^Version\s*:\s*(.+)$", result, re.MULTILINE)
                if version:
                    arch_version = version.group(1).strip()
                    arch_repo = repo
                    architecture = re.search(r"^Architecture\s*:\s*(.+)$", result, re.MULTILINE)
                    arch_arch = architecture.group(1).strip() if architecture else "x86_64"
                    break
            if not arch_version:
                continue
            checked.add(name)
            arch_versions[name] = arch_version
            if existing and int(run(["vercmp", upstream_version(arch_version),
                                     upstream_version(current["pkgver"])])) > 0:
                updates.add(name)
            target = metadata.get(name, {}).get("version", current["pkgver"])
            if int(run(["vercmp", upstream_version(arch_version), upstream_version(target)])) <= 0:
                continue
            if existing:
                continue
            marker = f"<!-- lunaos-buildbot:arch:{name}:{upstream_version(arch_version)} -->"
            if any(issue["title"] == title and (marker in issue.get("body", "") or
                   f"Arch has `{arch_version}`" in issue.get("body", "")) for issue in issues):
                continue
            source = owner["url"].removesuffix(".git")
            source_link = f"{source}/-/tree/HEAD" if "gitlab" in source else f"{source}/tree/HEAD"
            body = (
                f"Arch has `{arch_version}`, while LunaOS publishes `{current['pkgver']}`.\n\n"
                "Update this patched package to the current Arch version and carry its LunaOS changes forward.\n\n"
                f"[Package source]({source_link}) · [Arch package](https://archlinux.org/packages/{arch_repo}/{arch_arch}/{package_name}/)\n\n{marker}"
            )
            body_path = LOGDIR / f"{name}-arch-issue.md"
            body_path.write_text(body)
            url = run(["gh", "issue", "create", "--title", title, "--body-file", str(body_path)])
            issues.append({"number": int(url.rstrip("/").rsplit("/", 1)[-1]), "title": title, "state": "OPEN", "body": body})
            updates.add(name)
            log("arch-update", name=name, published=current["pkgver"], upstream=arch_version)
        except Exception as exc:
            log("arch-check-failed", name=package_name, error=str(exc))
    return updates, arch_versions


def check_nvchecker_updates(packages, published, metadata, arch_updates):
    updates, versions = set(), {}
    WORK.mkdir(parents=True, exist_ok=True)
    candidates = [(name, info) for name, info in metadata.items()
                  if info.get("nvchecker_config") and name in packages and
                  name not in arch_updates and lunaos_source(packages[name])]
    if not candidates:
        return updates, versions
    try:
        issues = json.loads(run(["gh", "issue", "list", "--state", "open", "--limit", "1000",
                                 "--json", "number,title,body"]))
    except Exception as exc:
        log("nvchecker-issues-unavailable", error=str(exc))
        issues = []
    for name, info in candidates:
        config_text = info.get("nvchecker_config")
        try:
            config = tomllib.loads(config_text)
            pkgbase = info.get("pkgbase", name)
            if pkgbase not in config:
                raise ValueError(f".nvchecker.toml has no [{pkgbase}] section")
            config_path = WORK / f"{name}.nvchecker.toml"
            config_path.write_text(config_text)
            output = run(["runuser", "-u", "user", "--", "env", "GIT_CONFIG_GLOBAL=/dev/null",
                          "GIT_CONFIG_SYSTEM=/dev/null", "nvchecker", "--file", config_path,
                          "--logger", "json", "--json-log-fd=1"], cwd=WORK)
            result = None
            for line in output.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("name") == pkgbase and event.get("event") in {"updated", "up-to-date"}:
                    result = event
                    break
            if not result:
                raise RuntimeError(f"nvchecker returned no version for {pkgbase}")
            version = result["version"]
            versions[name] = version
            published_version = next((published[pkg]["pkgver"] for pkg in info["names"] if pkg in published), None)
            source_version = info.get("version")
            current = source_version or published_version
            if current and published_version and int(run(["vercmp", published_version, current])) > 0:
                current = published_version
            if not current or int(run(["vercmp", version, upstream_version(current)])) <= 0:
                continue
            title = f"[update] {name}: update upstream package"
            if any(issue["title"] == title for issue in issues):
                updates.add(name)
                continue
            marker = f"<!-- lunaos-buildbot:nvchecker:{name}:{version} -->"
            body = (f"nvchecker found upstream version `{version}` for `{name}`. The package source declares "
                    f"`{upstream_version(source_version or current)}`; LunaOS publishes `{published_version or current}`.\n\n"
                    "Update the package source to the upstream version and carry LunaOS changes forward.\n\n"
                    f"[Package source]({packages[name]['url'].removesuffix('.git')})\n\n{marker}")
            body_path = LOGDIR / f"{name}-nvchecker-issue.md"
            body_path.write_text(body)
            url = run(["gh", "issue", "create", "--title", title, "--body-file", body_path])
            issue = {"number": int(url.rstrip("/").rsplit("/", 1)[-1]),
                     "title": title, "body": body}
            issues.append(issue)
            updates.add(name)
            log("nvchecker-update", current=upstream_version(current), upstream=version,
                issue=issue["number"])
        except Exception as exc:
            log("nvchecker-check-failed", name=name, error=str(exc))
    return updates, versions


def cleanup_issues(packages, published, metadata, arch_updates, arch_versions, metadata_only_changes=None):
    metadata_only_changes = metadata_only_changes or {}
    try:
        issues = json.loads(run(["gh", "issue", "list", "--state", "open", "--limit", "1000",
                                 "--json", "number,title,body"]))
    except Exception as exc:
        log("issue-cleanup-unavailable", error=str(exc))
        return

    def package_version(name):
        versions = [info["pkgver"] for package, info in published.items()
                    if (owner := packages.get(package) or packages.get(info.get("pkgbase", "")))
                    and owner["directory"] == name]
        return max(versions, key=cmp_to_key(lambda left, right: int(run(["vercmp", left, right])))) if versions else None

    for issue in issues:
        title, body = issue["title"], issue.get("body", "")
        match = re.fullmatch(r"\[(rebuild|update)\] ([^:]+): (bump pkgrel|sync Arch package|update upstream package)", title)
        if not match:
            continue
        kind, name, _ = match.groups()
        legacy_bot_issue = (
            kind == "rebuild" and body.startswith((
                f"A rebuild of `{name}` produced `",
                f"A declared dependency of `{name}` changed (",
                f"A source change for `{name}` still produces `",
            )) and
            "\n\nSource revision: `" in body or
            kind == "update" and body.startswith("Arch has `") and
            "Update this patched package to the current Arch version and carry its LunaOS changes forward." in body
        )
        reason = None
        if kind == "rebuild" and (legacy_bot_issue or f"<!-- lunaos-buildbot:rebuild:{name}:" in body):
            owner = packages.get(name)
            revision = re.search(r"Source revision: `([0-9a-f]+)`", body)
            abi_issue = "<!-- lunaos-buildbot:abi -->" in body or body.startswith("The Buildbot ABI/runtime scan found")
            if not abi_issue and revision and metadata_only_changes.get(name) == revision[1]:
                reason = "Obsolete: this source revision changes nvchecker metadata only."
            elif not owner or (not lunaos_source(owner) and not abi_issue):
                reason = "Obsolete: this package is no longer built from a LunaOS-maintained source."
            elif name in arch_updates:
                reason = "Superseded by the open Arch update issue."
            else:
                info = metadata.get(name, {})
                if abi_issue:
                    old = re.search(r"LunaOS publishes `([^`]+)`", body)
                    current = package_version(name)
                    if old and current and int(run(["vercmp", current, old[1]])) > 0:
                        reason = f"Resolved: LunaOS now publishes `{current}`."
                elif revision and published_source_matches(info, published, revision[1]):
                    reason = f"Resolved: published archive already contains source revision `{revision[1]}`."
                else:
                    old = (re.search(r"does not upgrade the published `([^`]+)`", body) or
                           re.search(r"same version LunaOS publishes `([^`]+)`", body) or
                           re.search(r"LunaOS currently publishes `([^`]+)`", body))
                    current = package_version(name)
                    if old and current and int(run(["vercmp", current, old[1]])) > 0:
                        reason = f"Resolved: LunaOS now publishes `{current}`."
        elif kind == "update" and f"<!-- lunaos-buildbot:nvchecker:{name}:" in body:
            owner = packages.get(name)
            if not owner or not lunaos_source(owner):
                reason = "Obsolete: this package is no longer built from a LunaOS-maintained source."
            elif name in arch_updates:
                reason = "Superseded by the Arch package update issue."
            else:
                target = re.search(rf"<!-- lunaos-buildbot:nvchecker:{re.escape(name)}:([^ >]+) -->", body)
                current = package_version(name)
                if target and current and int(run(["vercmp", upstream_version(current), target[1]])) >= 0:
                    reason = f"Resolved: LunaOS publishes `{current}`, which includes upstream `{target[1]}`."
        elif kind == "update" and (legacy_bot_issue or f"<!-- lunaos-buildbot:arch:{name}:" in body):
            owner = packages.get(name)
            if not owner or not lunaos_source(owner):
                reason = "Obsolete: this package is no longer built from a LunaOS-maintained source."
            current, upstream = package_version(name), arch_versions.get(name)
            if not reason and current and upstream and int(run(["vercmp", upstream_version(upstream), upstream_version(current)])) <= 0:
                reason = f"Resolved: LunaOS publishes `{current}`; Arch is at `{upstream}`."
        if reason:
            try:
                run(["gh", "issue", "close", str(issue["number"]), "--comment", reason])
                log("issue-closed", number=issue["number"], title=title, reason=reason)
            except Exception as exc:
                log("issue-close-failed", number=issue["number"], title=title, error=str(exc))


def installed_versions():
    result = {}
    for line in run(["pacman", "-Q"]).splitlines():
        name, _, version = line.partition(" ")
        result[name] = version.strip()
    return result


def dependency_versions(info, installed):
    versions = {}
    for name in info.get("deps", []):
        version = installed.get(name)
        if not version:
            continue
        if name == "python":
            version = version.rsplit(":", 1)[-1]
            match = re.match(r"(\d+\.\d+)", version)
            version = match.group(1) if match else version
        versions[name] = version
    return versions


def closure(seeds, dependents):
    selected, todo = set(seeds), list(seeds)
    while todo:
        for child in dependents.get(todo.pop(), ()):
            if child not in selected:
                selected.add(child)
                todo.append(child)
    return selected


def find_cycle(remaining, dependencies):
    state, stack, positions = {}, [], {}

    def visit(package):
        state[package] = 1
        positions[package] = len(stack)
        stack.append(package)
        for dependency in sorted(dependencies[package] & remaining):
            if state.get(dependency) == 1:
                return stack[positions[dependency]:]
            if not state.get(dependency):
                if cycle := visit(dependency):
                    return cycle
        stack.pop()
        positions.pop(package)
        state[package] = 2
        return []

    for package in sorted(remaining):
        if not state.get(package):
            if cycle := visit(package):
                return cycle
    return []


def order(selected, dependencies):
    result, remaining = [], set(selected)
    ordering_dependencies = {package: set(deps) for package, deps in dependencies.items()}
    while remaining:
        ready = sorted(p for p in remaining if not (ordering_dependencies[p] & remaining))
        if not ready:
            cycle = find_cycle(remaining, ordering_dependencies)
            package = min(cycle)
            dependency = min(ordering_dependencies[package] & set(cycle))
            ordering_dependencies[package].remove(dependency)
            log("bootstrap-cycle", cycle=cycle + [cycle[0]], consumer=package, dependency=dependency)
            continue
        result.extend(ready)
        remaining.difference_update(ready)
    return result


def emit_summary(results, changed, abi_triggers, selected):
    lines = ["## LunaOS package build", "",
             f"Sources changed: {len(changed)} · ABI rebuild issues: {len(abi_triggers)} · Selected: {len(selected)}",
             "", "| Package | Status | Details |", "|---|---|---|"]
    for name, result in sorted(results.items()):
        detail = RUNTIME.redact(result.get("detail", "")).replace("|", "\\|").replace("\n", "<br>")
        lines.append(f"| `{name}` | **{result['status']}** | {detail} |")
    text = "\n".join(lines) + "\n"
    (LOGDIR / "summary.md").write_text(text)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as stream:
            stream.write(text)


def outputs(**values):
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")


def load_state(packages):
    try:
        old = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
        if not isinstance(old, dict) or not isinstance(old.get("sources", {}), dict) or not isinstance(old.get("packages", {}), dict):
            raise ValueError("invalid state structure")
        for name, entry in old.get("packages", {}).items():
            if not isinstance(entry, dict):
                raise ValueError(f"invalid state for {name}")
            meta = entry.get("metadata", {})
            if not isinstance(meta, dict) or any(not isinstance(meta.get(k, []), list) or
                    not all(isinstance(v, str) for v in meta.get(k, [])) for k in ("names", "deps", "provides", "build_deps", "architectures")):
                raise ValueError(f"invalid metadata for {name}")
            if not isinstance(entry.get("dependency_versions", {}), dict):
                raise ValueError(f"invalid dependency versions for {name}")
        return ({k: v for k, v in old.get("sources", {}).items() if k in packages},
                {k: v for k, v in old.get("packages", {}).items() if k in packages})
    except (ValueError, TypeError) as exc:
        log("state-invalid", error=str(exc), action="rebuild from sources")
        return {}, {}


def available_versions():
    # Dependencies absent from the fresh image still need upgrade detection.
    versions = {}
    for line in run(["pacman", "-Sl"]).splitlines():
        fields = line.split()
        if len(fields) >= 3:
            versions.setdefault(fields[1], fields[2])  # pacman repository priority
    return {**installed_versions(), **versions}


def makepkg_options(flags):
    # Defer installation until archives pass validation/version checks.
    options, install = [], False
    for option in shlex.split(flags):
        if option == "--install":
            install = True
        elif option.startswith("-") and not option.startswith("--") and "i" in option:
            install = True
            if stripped := option.replace("i", "").lstrip("-"):
                options.append("-" + stripped)
        else:
            options.append(option)
    return options, install


def build_environment():
    return {key: value for key, value in os.environ.items() if not any(
        word in key.upper() for word in ("TOKEN", "PASSWORD", "PASSPHRASE", "PRIVATE_KEY", "SECRET"))}


def promote(built, infos, published, source):
    """Prepare all copies before replacing old archives; roll back split packages on error."""
    import tempfile
    bases = {info.get("pkgbase", info["pkgname"]) for info in infos}
    names = {info["pkgname"] for info in infos}
    obsolete = {name: info for name, info in published.items()
                if name in names or info.get("pkgbase", name) in bases}
    with tempfile.TemporaryDirectory(prefix=".promote-", dir=PKGDEST) as temp:
        temporary = Path(temp)
        for item, info in zip(built, infos):
            shutil.copy2(item, temporary / item.name)
            provenance = {"pkgname": info["pkgname"], "pkgver": info["pkgver"], **source}
            atomic_json(Path(str(item) + ".buildbot.json"), provenance)
            shutil.copy2(Path(str(item) + ".buildbot.json"), temporary / (item.name + ".buildbot.json"))
        backup = temporary / "old"
        backup.mkdir()
        replaced, saved = [], []
        try:
            for info in obsolete.values():
                old = Path(info["_path"])
                for item in (old, old.with_name(old.name + ".sig"), old.with_name(old.name + ".buildbot.json")):
                    if item.exists():
                        item.replace(backup / item.name)
                        saved.append(item)
            for item in built:
                target = PKGDEST / item.name
                if target.exists():
                    raise RuntimeError(f"unexpected destination archive: {target}")
                (temporary / item.name).replace(target)
                replaced.append(target)
                sidecar = target.with_name(target.name + ".buildbot.json")
                (temporary / sidecar.name).replace(sidecar)
                replaced.append(sidecar)
        except BaseException:
            for target in replaced:
                target.unlink(missing_ok=True)
            for target in saved:
                (backup / target.name).replace(target)
            raise
    for name in obsolete:
        published.pop(name)
    for item, info in zip(built, infos):
        published[info["pkgname"]] = dict(info, _path=str(PKGDEST / item.name),
                                          buildbot={"pkgname": info["pkgname"], "pkgver": info["pkgver"], **source})


def main():
    deadline = time.monotonic() + int(os.environ.get("BUILDBOT_RUN_TIMEOUT", "14400"))
    packages = read_manifest()
    requested = {item.strip() for item in os.environ.get("BUILDBOT_PACKAGES", "").split(",") if item.strip()}
    if unknown := requested - packages.keys():
        raise RuntimeError("unknown package directory: " + ", ".join(sorted(unknown)))
    sources, previous = load_state(packages)
    refs, changed, results, revisions = {}, set(), {}, {}
    metadata_only_changes = {}
    metadata = {name: previous.get(name, {}).get("metadata", {}) for name in packages}
    full = os.environ.get("BUILDBOT_FULL", "false").lower() == "true"
    published = published_packages()
    retry = {name for name, info in previous.items() if info.get("status") in FAILURES | {"deferred"}}

    def checkpoint():
        for name, result in results.items():
            if result["status"] in FAILURES | {"deferred"}:
                previous.setdefault(name, {})["status"] = result["status"]
        for name in packages:
            previous.setdefault(name, {})["metadata"] = metadata[name]
        atomic_json(STATE_PATH, {"sources": sources, "packages": previous,
                                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        atomic_json(LOGDIR / "results.json", results)

    for name, row in packages.items():
        with RUNTIME.context(name):
            try:
                refs[name] = source_sha(row)
                if refs[name] != sources.get(name):
                    changed.add(name)
                log("source-checked", revision=refs[name], previous=sources.get(name), changed=name in changed)
            except Exception as exc:
                results[name] = {"status": "source-check-failed", "detail": RUNTIME.redact(str(exc))}
                RUNTIME.exception("source-check-failed", exc)

    missing_metadata = {name for name in packages if not metadata[name].get("names")}
    inspect = set(packages) if full else changed | requested | retry | missing_metadata
    for name in sorted(inspect - results.keys()):
        with RUNTIME.context(name):
            try:
                path, revisions[name] = clone(packages[name], refs[name])
                metadata[name] = package_info(path)
                log("metadata", revision=revisions[name], **metadata[name])
            except Exception as exc:
                results[name] = {"status": "source-failed", "detail": RUNTIME.redact(str(exc))}
                RUNTIME.exception("metadata-failed", exc)

    for name, row in packages.items():
        if name in results or not refs.get(name) or metadata[name].get("nvchecker_revision") == refs[name]:
            continue
        try:
            if name not in revisions:
                metadata[name]["nvchecker_config"] = fetch_nvchecker_config(row, refs[name])
            metadata[name]["nvchecker_revision"] = refs[name]
            log("nvchecker-config-checked", found=bool(metadata[name].get("nvchecker_config")),
                revision=refs[name])
        except Exception as exc:
            RUNTIME.exception("nvchecker-config-failed", exc)

    arch_updates, arch_versions = check_arch_updates(packages, published, metadata)
    nvchecker_updates, nvchecker_versions = check_nvchecker_updates(
        packages, published, metadata, arch_updates)
    log("nvchecker-summary", checked=sorted(nvchecker_versions), updates=sorted(nvchecker_updates))
    cleanup_issues(packages, published, metadata, arch_updates, arch_versions)

    dependencies, dependents = graph(packages, metadata)
    available = available_versions()
    abi_triggers = elf_rebuild_triggers(packages, published, available, previous, refs, metadata)
    inspect |= set(abi_triggers)

    # A lost cache is not evidence of a source change. Compare versions before
    # compiling; otherwise every cold run builds the whole release and files
    # pkgrel issues for packages it has just downloaded at the same version.
    for name in sorted(inspect - results.keys()):
        info = metadata[name]
        issue_still_waiting = (previous.get(name, {}).get("abi_pending") and
                               previous.get(name, {}).get("pending_source") == refs[name])
        newer_source = bool(name in abi_triggers and info.get("version") and info.get("names") and all(
            pkg in published and int(run(["vercmp", info["version"], published[pkg]["pkgver"]])) > 0
            for pkg in info.get("names", [])))
        if name in abi_triggers and newer_source:
            changed.add(name)
        if name in abi_triggers and not newer_source and not full and name not in requested and not issue_still_waiting:
            number = None
            if name not in arch_updates:
                try:
                    number = create_pkgrel_issue(name, packages[name], refs[name],
                                                 info.get("version", abi_triggers[name]["published_version"]),
                                                 abi_triggers[name]["published_version"],
                                                 abi_triggers[name]["versions"],
                                                 abi_triggers[name]["reason"])
                except Exception as exc:
                    RUNTIME.exception("abi-rebuild-issue-failed", exc)
            detail = "ABI/runtime break detected; waiting for source update"
            if number:
                detail += f"; issue #{number}"
            elif name in arch_updates:
                detail += "; Arch update issue is already open"
            else:
                detail += "; issue creation failed"
            results[name] = {"status": "needs-pkgrel", "detail": detail}
            previous[name] = {**previous.get(name, {}), "pending_source": refs[name],
                              "status": "needs-pkgrel", "issue": number,
                              "abi_pending": number is not None or name in arch_updates}
            changed.discard(name)
            log("abi-rebuild-required", status="needs-pkgrel", triggers=abi_triggers[name], issue=number)
            continue
        if full or name in requested or not info.get("version"):
            continue
        if any(pkg not in published for pkg in info["names"]):
            continue
        with RUNTIME.context(name):
            comparisons = [int(run(["vercmp", info["version"], published[pkg]["pkgver"]])) for pkg in info["names"]]
            old_info = previous.get(name, {}).get("metadata", {})
            same_tree = info.get("tree") and info.get("tree") == old_info.get("tree")
            if sources.get(name) and not same_tree and info.get("dynamic_version"):
                continue  # pkgver() may discover a newer version during the build.
            if any(value > 0 for value in comparisons):
                continue
            # A release can be newer than the cache after a publish/cache failure.
            # Only an observed change *within the same declared version* proves
            # a missing pkgrel bump; never infer that from a stale cache alone.
            source_already_published = published_source_matches(info, published, refs[name])
            inputs_changed = False
            if sources.get(name) and refs[name] != sources[name] and not same_tree:
                try:
                    inputs_changed = build_inputs_changed(WORK / name, sources[name], refs[name])
                    if not inputs_changed:
                        metadata_only_changes[name] = refs[name]
                except Exception as exc:
                    log("source-diff-unavailable", previous=sources[name], current=refs[name], error=str(exc))
            known_change = bool((sources.get(name) and refs[name] != sources[name] and not same_tree and
                                 old_info.get("version") == info["version"] and
                                 inputs_changed and not source_already_published))
            if known_change and any(value == 0 for value in comparisons):
                number = None
                if lunaos_source(packages[name]):
                    detail = "source or tracked dependency changed without a newer package version; compilation skipped"
                    if name in arch_updates:
                        detail += "; rebuild issue suppressed because an Arch update issue is open"
                    else:
                        try:
                            number = create_pkgrel_issue(name, packages[name], refs[name], info["version"],
                                                         info["version"], {})
                        except Exception as exc:
                            RUNTIME.exception("pkgrel-issue-failed", exc)
                else:
                    detail = "AUR source produced the published version; no issue opened because LunaOS does not maintain this PKGBUILD"
                results[name] = {"status": "needs-pkgrel", "detail": detail}
                previous[name] = {**previous.get(name, {}), "pending_source": refs[name], "status": "needs-pkgrel", "issue": number}
            else:
                results[name] = {"status": "up-to-date", "detail": "published version already satisfies source; compilation skipped"}
                sources[name] = refs[name]
                previous[name] = {"metadata": info, "status": "up-to-date",
                                  "dependency_versions": dependency_versions(info, available)}
            changed.discard(name)
            log("build-unnecessary", status=results[name]["status"], version=info["version"],
                same_tree=bool(same_tree), build_inputs_changed=inputs_changed,
                source_already_published=source_already_published)

    missing_archives = {name for name, info in metadata.items() if info.get("names") and
                        any(pkg not in published for pkg in info["names"])}
    skipped = {name for name, result in results.items() if result["status"] in {"up-to-date", "needs-pkgrel"}}
    seeds = set(packages) if full else (changed | requested | retry | missing_metadata | missing_archives | results.keys()) - skipped
    # Runtime-only changes do not require rebuilding metapackages, themes or settings.
    rebuild_dependents = {name: {child for child in children if
        metadata[child].get("architectures") != ["any"] or
        (set(metadata[name].get("names", [])) | set(metadata[name].get("provides", []))) &
        (set(metadata[child].get("build_deps", [])) | {"python"})} for name, children in dependents.items()}
    selected = closure(seeds, rebuild_dependents) - skipped
    build_order = order(selected, dependencies)
    atomic_json(LOGDIR / "plan.json", {"order": build_order, "dependencies": {k: sorted(v) for k, v in dependencies.items()},
                                      "changed": sorted(changed), "requested": sorted(requested), "retry": sorted(retry),
                                      "missing_archives": sorted(missing_archives), "skipped": sorted(skipped),
                                      "abi_triggers": abi_triggers})
    log("build-plan", selected=len(selected), order=build_order)
    checkpoint()
    firmware = Path(os.environ.get("FIRMWARE_TARBALL", "/tmp/lunaos-input/firmware.tar"))
    for name in build_order:
        with RUNTIME.context(name):
            if name in results:
                log("source-unavailable", status=results[name]["status"])
                continue
            blocked = sorted(dep for dep in dependencies[name] if results.get(dep, {}).get("status") in FAILURES)
            if blocked:
                results[name] = {"status": "blocked", "detail": "failed dependencies: " + ", ".join(blocked)}
                log("blocked", dependencies=blocked)
                checkpoint()
                continue
            if time.monotonic() >= deadline:
                results[name] = {"status": "deferred", "detail": "run time budget exhausted; retry on next run"}
                log("build-deferred", detail=results[name]["detail"])
                checkpoint()
                continue
            row, stage = packages[name], WORK / "packages" / name
            start = time.monotonic()
            try:
                log("build-start", source=row["url"], revision=refs[name], dependencies=sorted(dependencies[name]))
                # Never reuse an unverified leftover checkout from an interrupted run.
                if name not in revisions:
                    path, revisions[name] = clone(row, refs[name])
                    metadata[name] = package_info(path)
                else:
                    path = WORK / name
                if name == "linux-firmware-apple":
                    if not firmware.is_file():
                        raise RuntimeError(f"required firmware archive is missing: {firmware}")
                    shutil.copy2(firmware, path / "firmware.tar")
                    run(["chown", "user:user", str(path / "firmware.tar")])
                # Bootstrap cycles/unchanged local dependencies from the downloaded release.
                local = []
                for dependency in sorted(dependencies[name]):
                    if results.get(dependency, {}).get("status") == "built":
                        continue
                    for pkg in metadata[dependency].get("names", []):
                        if pkg in published:
                            local.append(published[pkg]["_path"])
                if local:
                    run(["pacman", "-U", "--noconfirm", "--needed", "--asdeps", *sorted(set(local))])
                if row["before"]:
                    run(["bash", "-e", "-o", "pipefail", "-c", row["before"]], cwd=path)
                if stage.exists():
                    shutil.rmtree(stage)
                stage.mkdir(parents=True)
                run(["chown", "user:user", str(stage)])
                flags, install = makepkg_options(row["makepkg_flags"])
                environment = build_environment()
                environment["PKGDEST"] = str(stage)
                run(["runuser", "-u", "user", "--", "makepkg", "--noconfirm", "--nocolor", *flags],
                    cwd=path, env=environment, capture=False,
                    timeout=max(1, min(int(os.environ.get("BUILDBOT_BUILD_TIMEOUT", "7200")),
                                       deadline - time.monotonic())))
                built = sorted(stage.glob("*.pkg.tar.zst"))
                if not built:
                    raise RuntimeError("makepkg produced no package archives")
                infos = [archive_info(item) for item in built]
                if len({info["pkgname"] for info in infos}) != len(infos):
                    raise RuntimeError("makepkg produced duplicate package names")
                if missing := set(metadata[name]["names"]) - {info["pkgname"] for info in infos}:
                    raise RuntimeError("missing split package archives: " + ", ".join(sorted(missing)))
                stale = [(info, published[info["pkgname"]]) for info in infos if info["pkgname"] in published
                         and int(run(["vercmp", info["pkgver"], published[info["pkgname"]]["pkgver"]])) <= 0]
                versions = dependency_versions(metadata[name], installed_versions())
                if stale:
                    info, old = stale[0]
                    number = None
                    actionable = bool(name in changed and sources.get(name))
                    managed = lunaos_source(row)
                    if actionable and managed and name not in arch_updates:
                        try:
                            number = create_pkgrel_issue(name, row, revisions[name], info["pkgver"], old["pkgver"], {})
                        except Exception as exc:
                            RUNTIME.exception("pkgrel-issue-failed", exc)
                    status = "needs-pkgrel" if actionable else "unchanged"
                    detail = f"no newer archive ({info['pkgver']} ≤ {old['pkgver']})" + (f"; issue #{number}" if number else "")
                    if actionable and not managed:
                        detail += "; source is AUR and is not maintained by LunaOS, so no issue was opened"
                    elif actionable and name in arch_updates:
                        detail += "; rebuild issue suppressed because an Arch update issue is open"
                    results[name] = {"status": status, "detail": detail}
                    previous[name] = {"metadata": metadata[name], "dependency_versions": versions,
                                      "pending_source": revisions[name], "issue": number, "status": status}
                    # sources continues to describe the published archive, not this rejected build.
                    log(status, detail=detail)
                    continue
                if install or dependents[name] & selected:
                    run(["pacman", "-U", "--noconfirm", "--asdeps", *map(str, built)])
                if row["after"]:
                    run(["bash", "-e", "-o", "pipefail", "-c", row["after"]], cwd=path)
                promote(built, infos, published, {"source_url": row["url"], "source_revision": revisions[name],
                                                   "source_tree": metadata[name].get("tree")})
                results[name] = {"status": "built", "detail": ", ".join(p.name for p in built)}
                sources[name] = revisions[name]
                previous[name] = {"metadata": metadata[name], "dependency_versions": versions, "status": "built",
                                  "built_at": dt.datetime.now(dt.timezone.utc).isoformat()}
                log("build-success", archives=[p.name for p in built])
            except Exception as exc:
                results[name] = {"status": "failed", "detail": RUNTIME.redact(str(exc))}
                RUNTIME.exception("build-failed", exc)
            finally:
                results.setdefault(name, {"status": "failed", "detail": "build interrupted"})
                results[name]["elapsed_seconds"] = round(time.monotonic() - start, 3)
                checkpoint()
                # Compilers/kernel trees otherwise accumulate until the runner runs out of disk.
                for scratch in (WORK / name, stage):
                    if scratch.exists():
                        try:
                            shutil.rmtree(scratch)
                        except OSError as exc:
                            log("cleanup-failed", path=str(scratch), error=str(exc))
                log("build-end", status=results[name]["status"], elapsed_seconds=results[name]["elapsed_seconds"],
                    free_bytes=shutil.disk_usage(WORK).free)
    cleanup_issues(packages, published, metadata, arch_updates, arch_versions,
                   metadata_only_changes)
    checkpoint()
    emit_summary(results, changed, abi_triggers, selected)
    failed = sum(result["status"] in FAILURES for result in results.values())
    built = sum(result["status"] == "built" for result in results.values())
    deferred = sum(result["status"] == "deferred" for result in results.values())
    outputs(failed=failed, packages_built=built, deferred=deferred, fatal=0)
    log("run-end", failed=failed, packages_built=built, deferred=deferred)
    # Actions publishes the successful subset before its final failure gate.
    return 0 if os.environ.get("GITHUB_OUTPUT") else int(failed > 0)


def entrypoint():
    global RUNTIME
    try:
        RUNTIME = Runtime(LOGDIR)
        outputs(failed=1, packages_built=0, fatal=1)
        for directory in (WORK, PKGDEST, STATE_PATH.parent):
            directory.mkdir(parents=True, exist_ok=True)
        log("run-start", python=sys.version, work=str(WORK), destination=str(PKGDEST), state=str(STATE_PATH),
            free_bytes=shutil.disk_usage(WORK).free)
        return main()
    except BaseException as exc:
        if RUNTIME:
            RUNTIME.exception("fatal-error", exc)
            atomic_json(LOGDIR / "fatal.json", {"error": RUNTIME.redact(str(exc)), "type": type(exc).__name__})
        else:
            print(f"buildbot initialization failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"received signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(entrypoint())
