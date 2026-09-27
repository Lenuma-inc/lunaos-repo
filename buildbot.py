#!/usr/bin/env python3
"""Incremental Arch package builder with dependency-aware scheduling."""

import csv
import datetime as dt
import json
import os
import re
import shlex
import shutil
import signal
import sys
import time
from pathlib import Path

from buildbot_runtime import Runtime, atomic_json

ROOT = Path(__file__).resolve().parent
WORK = Path(os.environ.get("BUILDBOT_WORK", "/tmp/lunaos-buildbot"))
PKGDEST = Path(os.environ.get("PKGDEST", "/tmp/lunaos-repo"))
LOGDIR = Path(os.environ.get("BUILDBOT_LOGDIR", "/tmp/lunaos-build-logs"))
STATE_PATH = Path(os.environ.get("BUILDBOT_STATE", ROOT / ".build-state" / "state.json"))
RUNTIME = None
FAILURES = {"failed", "blocked", "source-failed", "source-check-failed"}


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


def srcinfo(path):
    file = path / ".SRCINFO"
    if (path / "PKGBUILD").is_file():
        # A stale committed .SRCINFO must not hide a new PKGBUILD version.
        text = run(["runuser", "-u", "user", "--", "makepkg", "--printsrcinfo"], cwd=path, env=build_environment())
    else:
        text = file.read_text()
    deps, build_deps, provides, names, architectures = set(), set(), set(), set(), set()
    version = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if not sep:
            continue
        if key == "pkgname":
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
    if version.get("pkgver") and version.get("pkgrel"):
        epoch = version.get("epoch", "0")
        info["version"] = (f"{epoch}:" if epoch != "0" else "") + version["pkgver"] + "-" + version["pkgrel"]
    pkgbuild = (path / "PKGBUILD").read_text() if (path / "PKGBUILD").exists() else ""
    info["dynamic_version"] = bool(re.search(r"(?m)^\s*(?:function\s+)?pkgver\s*\(\)", pkgbuild))
    return info


def package_info(path):
    try:
        info = srcinfo(path)
        info["tree"] = run(["runuser", "-u", "user", "--", "git", "rev-parse", "HEAD^{tree}"], cwd=path)
        return info
    except Exception as exc:
        raise RuntimeError(f"cannot read .SRCINFO: {exc}") from exc


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
    if not all(parsed.get(key) for key in ("pkgname", "pkgver", "arch")):
        raise RuntimeError(f"invalid package archive: {path}")
    return parsed


def published_packages():
    result = {}
    for path in sorted(PKGDEST.glob("*.pkg.tar.zst")):
        if info := archive_info(path):
            if name := info.get("pkgname"):
                if name in result:
                    raise RuntimeError(f"multiple published archives for {name}: {result[name]['_path']}, {path}")
                info["_path"] = str(path)
                result[name] = info
    return result


def create_pkgrel_issue(name, row, revision, current, published, dependency_changes):
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
    body = (
        f"A rebuild of `{name}` produced `{current}`, which does not upgrade the published `{published}`.\n\n"
        f"Please bump `pkgrel` (or update `pkgver` if needed) in the [package source]({source_link}) and push the change. "
        "The buildbot will retry after the source changes.\n\n"
        f"Trigger: {changed}\n\nSource revision: `{revision}`\n\n{marker}"
    )
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
        return
    checked = set()
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
            log("arch-update", name=name, published=current["pkgver"], upstream=arch_version)
        except Exception as exc:
            log("arch-check-failed", name=package_name, error=str(exc))


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


def emit_summary(results, changed, dependency_changes, selected):
    lines = ["## LunaOS package build", "",
             f"Sources changed: {len(changed)} · Dependencies changed: {len(dependency_changes)} · Selected: {len(selected)}",
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


def promote(built, infos, published):
    """Prepare all copies before replacing old archives; roll back split packages on error."""
    import tempfile
    bases = {info.get("pkgbase", info["pkgname"]) for info in infos}
    names = {info["pkgname"] for info in infos}
    obsolete = {name: info for name, info in published.items()
                if name in names or info.get("pkgbase", name) in bases}
    with tempfile.TemporaryDirectory(prefix=".promote-", dir=PKGDEST) as temp:
        temporary = Path(temp)
        for item in built:
            shutil.copy2(item, temporary / item.name)
        backup = temporary / "old"
        backup.mkdir()
        replaced, saved = [], []
        try:
            for info in obsolete.values():
                old = Path(info["_path"])
                for item in (old, old.with_name(old.name + ".sig")):
                    if item.exists():
                        item.replace(backup / item.name)
                        saved.append(item)
            for item in built:
                target = PKGDEST / item.name
                if target.exists():
                    raise RuntimeError(f"unexpected destination archive: {target}")
                (temporary / item.name).replace(target)
                replaced.append(target)
        except BaseException:
            for target in replaced:
                target.unlink(missing_ok=True)
            for target in saved:
                (backup / target.name).replace(target)
            raise
    for name in obsolete:
        published.pop(name)
    for item, info in zip(built, infos):
        published[info["pkgname"]] = dict(info, _path=str(PKGDEST / item.name))


def main():
    deadline = time.monotonic() + int(os.environ.get("BUILDBOT_RUN_TIMEOUT", "14400"))
    packages = read_manifest()
    requested = {item.strip() for item in os.environ.get("BUILDBOT_PACKAGES", "").split(",") if item.strip()}
    if unknown := requested - packages.keys():
        raise RuntimeError("unknown package directory: " + ", ".join(sorted(unknown)))
    sources, previous = load_state(packages)
    refs, changed, results, revisions = {}, set(), {}, {}
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
                if refs[name] != sources.get(name) and refs[name] != previous.get(name, {}).get("pending_source"):
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

    dependencies, dependents = graph(packages, metadata)
    available = available_versions()
    dependency_changes = {}
    rebuild_deps = {dep.strip() for dep in os.environ.get("BUILDBOT_REBUILD_DEPS", "python").split(",") if dep.strip()}
    for name, info in metadata.items():
        old_versions = previous.get(name, {}).get("dependency_versions", {})
        current = dependency_versions(info, available)
        diff = {dep: (old_versions[dep], version) for dep, version in current.items()
                if dep in rebuild_deps and dep in old_versions and old_versions[dep] != version}
        # A pkgrel request is retried only after its source changes or manual selection.
        if diff and previous.get(name, {}).get("status") != "needs-pkgrel":
            dependency_changes[name] = diff

    # A lost cache is not evidence of a source change. Compare versions before
    # compiling; otherwise every cold run builds the whole release and files
    # pkgrel issues for packages it has just downloaded at the same version.
    for name in sorted(inspect - results.keys()):
        info = metadata[name]
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
            known_change = bool((sources.get(name) and refs[name] != sources[name] and not same_tree and
                                 old_info.get("version") == info["version"]) or name in dependency_changes)
            if known_change and any(value == 0 for value in comparisons):
                number = None
                if os.environ.get("BUILDBOT_ISSUES", "false").lower() == "true":
                    try:
                        number = create_pkgrel_issue(name, packages[name], refs[name], info["version"],
                                                     info["version"], {})
                    except Exception as exc:
                        RUNTIME.exception("pkgrel-issue-failed", exc)
                results[name] = {"status": "needs-pkgrel", "detail": "source or tracked dependency changed without a newer package version; compilation skipped"}
                previous[name] = {**previous.get(name, {}), "pending_source": refs[name], "status": "needs-pkgrel", "issue": number}
            else:
                results[name] = {"status": "up-to-date", "detail": "published version already satisfies source; compilation skipped"}
                sources[name] = refs[name]
                previous[name] = {"metadata": info, "status": "up-to-date",
                                  "dependency_versions": dependency_versions(info, available)}
            changed.discard(name)
            log("build-unnecessary", status=results[name]["status"], version=info["version"], same_tree=bool(same_tree))

    missing_archives = {name for name, info in metadata.items() if info.get("names") and
                        any(pkg not in published for pkg in info["names"])}
    skipped = {name for name, result in results.items() if result["status"] in {"up-to-date", "needs-pkgrel"}}
    seeds = set(packages) if full else (changed | requested | retry | missing_metadata | missing_archives | set(dependency_changes) | results.keys()) - skipped
    # Runtime-only changes do not require rebuilding metapackages, themes or settings.
    rebuild_dependents = {name: {child for child in children if
        metadata[child].get("architectures") != ["any"] or
        (set(metadata[name].get("names", [])) | set(metadata[name].get("provides", []))) &
        (set(metadata[child].get("build_deps", [])) | rebuild_deps)} for name, children in dependents.items()}
    selected = closure(seeds, rebuild_dependents) - skipped
    build_order = order(selected, dependencies)
    atomic_json(LOGDIR / "plan.json", {"order": build_order, "dependencies": {k: sorted(v) for k, v in dependencies.items()},
                                      "changed": sorted(changed), "requested": sorted(requested), "retry": sorted(retry),
                                      "missing_archives": sorted(missing_archives), "skipped": sorted(skipped),
                                      "dependency_changes": dependency_changes})
    log("build-plan", selected=len(selected), order=build_order)
    checkpoint()
    firmware = Path(os.environ.get("FIRMWARE_TARBALL", "/tmp/lunaos-input/firmware.tar"))
    if os.environ.get("BUILDBOT_ARCH_ISSUES", "false").lower() == "true":
        check_arch_updates(packages, published, metadata)

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
                    actionable = bool((name in changed and sources.get(name)) or name in dependency_changes)
                    if actionable and os.environ.get("BUILDBOT_ISSUES", "false").lower() == "true":
                        try:
                            number = create_pkgrel_issue(name, row, revisions[name], info["pkgver"], old["pkgver"], dependency_changes.get(name, {}))
                        except Exception as exc:
                            RUNTIME.exception("pkgrel-issue-failed", exc)
                    status = "needs-pkgrel" if actionable else "unchanged"
                    detail = f"no newer archive ({info['pkgver']} ≤ {old['pkgver']})" + (f"; issue #{number}" if number else "")
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
                promote(built, infos, published)
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
    checkpoint()
    emit_summary(results, changed, dependency_changes, selected)
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
