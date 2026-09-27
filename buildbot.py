#!/usr/bin/env python3
"""Incremental Arch package builder with dependency-aware scheduling."""

import csv
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

ROOT = Path(__file__).resolve().parent
WORK = Path(os.environ.get("BUILDBOT_WORK", "/tmp/lunaos-buildbot"))
PKGDEST = Path(os.environ.get("PKGDEST", "/tmp/lunaos-repo"))
LOGDIR = Path(os.environ.get("BUILDBOT_LOGDIR", "/tmp/lunaos-build-logs"))
STATE_PATH = Path(os.environ.get("BUILDBOT_STATE", ROOT.parent / ".build-state" / "state.json"))
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
WORK.mkdir(parents=True, exist_ok=True)
PKGDEST.mkdir(parents=True, exist_ok=True)
LOGDIR.mkdir(parents=True, exist_ok=True)


def run(args, *, cwd=None, check=True):
    result = subprocess.run(args, cwd=cwd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and result.returncode:
        raise RuntimeError(f"{shlex.join(args)} exited {result.returncode}: {result.stdout.strip()}")
    return result.stdout.strip()


def read_manifest():
    with (ROOT / "packages.tsv").open(newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    if not rows or len({row["directory"] for row in rows}) != len(rows):
        raise RuntimeError("packages.tsv is empty or contains duplicate directories")
    return {row["directory"]: row for row in rows}


def source_sha(row):
    ref = "HEAD" if row["ref"] == "HEAD" else f"refs/heads/{row['ref']}"
    output = run(["git", "ls-remote", "--symref", row["url"], ref])
    target = "HEAD" if row["ref"] == "HEAD" else ref
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[-1] == target and fields[0] != "ref:":
            return fields[0]
    raise RuntimeError(f"remote ref not found: {row['url']} {row['ref']}")


def clone(row):
    path = WORK / row["directory"]
    if path.exists():
        shutil.rmtree(path)
    args = ["git", "clone", "--depth", "1"]
    if row["ref"] != "HEAD":
        args += ["--branch", row["ref"], "--single-branch"]
    args += [row["url"], str(path)]
    run(args)
    revision = run(["git", "rev-parse", "HEAD"], cwd=path)
    run(["chown", "-R", "user:user", str(path)])
    return path, revision


def srcinfo(path):
    file = path / ".SRCINFO"
    if not file.exists():
        text = run(["runuser", "-u", "user", "--", "makepkg", "--printsrcinfo"], cwd=path)
    else:
        text = file.read_text()
    deps, provides, names = set(), set(), set()
    for line in text.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if not sep:
            continue
        if key == "pkgname":
            names.add(value)
        elif key in {"depends", "makedepends", "checkdepends"} or key.startswith(("depends_", "makedepends_", "checkdepends_")):
            deps.add(re.split(r"[<>=]", value, maxsplit=1)[0])
        elif key == "provides" or key.startswith("provides_"):
            provides.add(re.split(r"[<>=]", value, maxsplit=1)[0])
    return {"deps": sorted(deps), "provides": sorted(provides), "names": sorted(names)}


def package_info(path):
    try:
        return srcinfo(path)
    except Exception as exc:
        raise RuntimeError(f"cannot read .SRCINFO: {exc}") from exc


def graph(packages, metadata):
    names, provides = {}, {}
    for directory, info in metadata.items():
        for name in info.get("names", []):
            names.setdefault(name, set()).add(directory)
        for name in info.get("provides", []):
            provides.setdefault(name, set()).add(directory)
    dependencies = {directory: set() for directory in packages}
    dependents = {directory: set() for directory in packages}
    for directory, info in metadata.items():
        for name in info.get("deps", []):
            # The build image already supplies this group; depending on its meta-package can create bootstrap cycles.
            if name == "base-devel":
                continue
            providers = names.get(name) or provides.get(name, set())
            if len(providers) == 1 and (dependency := next(iter(providers))) != directory:
                dependencies[directory].add(dependency)
                dependents[dependency].add(directory)
    return dependencies, dependents


def archive_info(path):
    info = run(["bsdtar", "-xOf", str(path), ".PKGINFO"], check=False)
    return {key: value for line in info.splitlines() if (key := line.partition(" = ")[0]) and (value := line.partition(" = ")[2])}


def published_packages():
    result = {}
    for path in PKGDEST.glob("*.pkg.tar.zst"):
        if info := archive_info(path):
            if name := info.get("pkgname"):
                info["_path"] = str(path)
                result[name] = info
    return result


def create_pkgrel_issue(name, row, revision, current, published, dependency_changes):
    title = f"[rebuild] {name}: bump pkgrel"
    issues = json.loads(run(["gh", "issue", "list", "--state", "open", "--limit", "1000", "--json", "number,title"]))
    existing = next((issue for issue in issues if issue["title"] == title), None)
    if existing:
        return existing["number"]
    changed = ", ".join(f"`{dep}` {old} → {new}" for dep, (old, new) in dependency_changes.items()) or "package source changed"
    source = row["url"].removesuffix(".git")
    source_link = f"{source}/-/tree/{revision}" if "gitlab" in source else f"{source}/tree/{revision}"
    body = (
        f"A rebuild of `{name}` produced `{current}`, which does not upgrade the published `{published}`.\n\n"
        f"Please bump `pkgrel` (or update `pkgver` if needed) in the [package source]({source_link}) and push the change. "
        "The buildbot will retry after the source changes.\n\n"
        f"Trigger: {changed}\n\nSource revision: `{revision}`"
    )
    url = run(["gh", "issue", "create", "--title", title, "--body", body])
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def check_arch_updates(packages, published):
    try:
        issues = json.loads(run(["gh", "issue", "list", "--state", "open", "--limit", "1000", "--json", "number,title"]))
    except Exception as exc:
        print(f"::warning::Could not list Arch update issues: {exc}")
        return
    for package_name, current in published.items():
        owner = packages.get(package_name) or packages.get(current.get("pkgbase", ""))
        if not owner:
            continue
        try:
            name = owner["directory"]
            title = f"[update] {name}: sync Arch package"
            existing = next((issue for issue in issues if issue["title"] == title), None)
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
            if int(run(["vercmp", arch_version, current["pkgver"]])) <= 0:
                if existing:
                    run(["gh", "issue", "close", str(existing["number"]), "--comment", "The LunaOS package now matches or exceeds Arch."])
                continue
            if existing:
                continue
            source = owner["url"].removesuffix(".git")
            source_link = f"{source}/-/tree/HEAD" if "gitlab" in source else f"{source}/tree/HEAD"
            body = (
                f"Arch has `{arch_version}`, while LunaOS publishes `{current['pkgver']}`.\n\n"
                "Update this patched package to the current Arch version and carry its LunaOS changes forward.\n\n"
                f"[Package source]({source_link}) · [Arch package](https://archlinux.org/packages/{arch_repo}/{arch_arch}/{package_name}/)"
            )
            url = run(["gh", "issue", "create", "--title", title, "--body", body])
            issues.append({"number": int(url.rstrip("/").rsplit("/", 1)[-1]), "title": title})
            print(f"::notice::Arch update available for {name}: {current['pkgver']} → {arch_version}")
        except Exception as exc:
            print(f"::warning::Could not check Arch version for {package_name}: {exc}")


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
            print(f"::warning title=Bootstrap dependency cycle::{package} will build using the published {dependency}")
            continue
        result.extend(ready)
        remaining.difference_update(ready)
    return result


def emit_summary(results, changed, dependency_changes, selected):
    summary = Path(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"))
    lines = ["## LunaOS package build", "", f"Sources changed: {len(changed)} · Dependencies changed: {len(dependency_changes)} · Selected: {len(selected)}", "", "| Package | Status | Details |", "|---|---|---|"]
    for name, result in sorted(results.items()):
        lines.append(f"| `{name}` | **{result['status']}** | {result.get('detail', '')} |")
    with summary.open("a") as f:
        f.write("\n".join(lines) + "\n")


def main():
    packages = read_manifest()
    old = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {"sources": {}, "packages": {}}
    sources = dict(old.get("sources", {}))
    previous = dict(old.get("packages", {}))
    refs, changed, results = {}, set(), {}
    print(f"[buildbot] checking sources for {len(packages)} packages")

    for name, row in packages.items():
        try:
            print(f"[source] checking {name}")
            refs[name] = source_sha(row)
            pending = previous.get(name, {})
            if refs[name] != sources.get(name) and refs[name] != pending.get("pending_source"):
                changed.add(name)
                print(f"[source] changed {name}: {refs[name][:12]}")
            else:
                print(f"[source] unchanged {name}: {refs[name][:12]}")
        except Exception as exc:
            results[name] = {"status": "source-check-failed", "detail": str(exc)}
            (LOGDIR / f"{name}.log").write_text(f"source check failed: {exc}\n")
            print(f"::error title=Source check failed::{name}: {exc}")

    metadata = {name: previous.get(name, {}).get("metadata", {}) for name in packages}
    full = os.environ.get("BUILDBOT_FULL", "false").lower() == "true" or not all(metadata.values())
    requested = {item.strip() for item in os.environ.get("BUILDBOT_PACKAGES", "").split(",") if item.strip()}
    unknown = requested - set(packages)
    if unknown:
        raise RuntimeError("unknown package directory: " + ", ".join(sorted(unknown)))

    # Refresh metadata before computing reverse dependencies for changed PKGBUILDs.
    inspect = set(packages) if full else changed | requested
    revisions = {}
    for name in sorted(inspect):
        try:
            print(f"[metadata] reading {name}")
            path, revision = clone(packages[name])
            revisions[name] = revision
            metadata[name] = package_info(path)
            print(f"[metadata] {name}: {len(metadata[name]['names'])} package name(s), {len(metadata[name]['deps'])} dependencies")
        except Exception as exc:
            results[name] = {"status": "source-failed", "detail": str(exc)}
            (LOGDIR / f"{name}.log").write_text(f"source checkout failed: {exc}\n")
            changed.add(name)
            print(f"::error title=Metadata failed::{name}: {exc}")

    dependencies, dependents = graph(packages, metadata)
    print(f"[graph] {sum(map(len, dependencies.values()))} dependency edges across {len(packages)} packages")
    installed = installed_versions()
    dependency_changes = {}
    for name, info in metadata.items():
        old_versions = previous.get(name, {}).get("dependency_versions", {})
        current_versions = dependency_versions(info, installed)
        diff = {dep: (old_versions[dep], version) for dep, version in current_versions.items() if dep in old_versions and old_versions[dep] != version}
        if diff:
            dependency_changes[name] = diff
    retry = {name for name, info in previous.items() if info.get("status") in {"failed", "blocked", "source-failed", "source-check-failed"}}
    seeds = set(packages) if full else changed | set(dependency_changes) | requested | retry
    selected = closure(seeds, dependents)
    for name in packages:
        if name in results and name not in selected:
            selected.add(name)
    print(f"[plan] {len(selected)} packages selected: {', '.join(sorted(selected))}")
    try:
        build_order = order(selected, dependencies)
    except RuntimeError as exc:
        print(f"::error title=Build plan failed::{exc}")
        summary = Path(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"))
        with summary.open("a") as f:
            f.write("## Build plan failed\n\n```text\n" + str(exc) + "\n```\n")
        raise
    print(f"[plan] build order: {' → '.join(build_order)}")
    firmware = Path(os.environ.get("FIRMWARE_TARBALL", "/tmp/lunaos-input/firmware.tar"))
    published = published_packages()
    check_arch_updates(packages, published)

    for name in build_order:
        if name in results and results[name]["status"] in {"source-failed", "source-check-failed"}:
            print(f"[skip] {name}: source unavailable")
            continue
        blocked = sorted(dep for dep in dependencies[name] if dep in results and results[dep]["status"] != "built")
        if blocked:
            results[name] = {"status": "blocked", "detail": "failed dependencies: " + ", ".join(blocked)}
            (LOGDIR / f"{name}.log").write_text(results[name]["detail"] + "\n")
            print(f"[blocked] {name}: failed dependencies: {', '.join(blocked)}")
            continue
        row = packages[name]
        path = WORK / name
        stage = WORK / "packages" / name
        log_path = LOGDIR / f"{name}.log"
        try:
            print(f"[build] starting {name}")
            if not path.exists():
                path, revisions[name] = clone(row)
                metadata[name] = package_info(path)
            if name == "linux-firmware-apple" and firmware.exists():
                (path / "firmware.tar").write_bytes(firmware.read_bytes())
                run(["chown", "user:user", str(path / "firmware.tar")])
            if row["before"]:
                run(shlex.split(row["before"]))
            if stage.exists():
                shutil.rmtree(stage)
            stage.mkdir(parents=True)
            run(["chown", "user:user", str(stage)])
            args = ["runuser", "-u", "user", "--", "makepkg", "--noconfirm", *shlex.split(row["makepkg_flags"])]
            with log_path.open("w") as log:
                log.write(f"source: {row['url']} @ {revisions.get(name, refs.get(name, 'unknown'))}\n")
                env = dict(os.environ, PKGDEST=str(stage))
                proc = subprocess.Popen(args, cwd=path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                for line in proc.stdout:
                    sys.stdout.write(f"[{name}] {line}")
                    log.write(line)
                if proc.wait():
                    raise RuntimeError(f"makepkg failed; see {log_path}")
                built = list(stage.glob("*.pkg.tar.zst"))
            if not built:
                raise RuntimeError("makepkg produced no package archives")
            built_info = [archive_info(package) for package in built]
            stale = [(info, published.get(info.get("pkgname", ""))) for info in built_info]
            stale = [(info, old) for info, old in stale if old and int(run(["vercmp", info.get("pkgver", ""), old.get("pkgver", "")])) <= 0]
            if stale:
                info, old = stale[0]
                number = create_pkgrel_issue(name, row, revisions.get(name, refs.get(name, "unknown")), info["pkgver"], old["pkgver"], dependency_changes.get(name, {}))
                detail = f"needs pkgrel bump; issue #{number} ({info['pkgver']} ≤ {old['pkgver']})"
                results[name] = {"status": "needs-pkgrel", "detail": detail}
                previous[name] = {
                    "metadata": metadata.get(name, {}),
                    "dependency_versions": dependency_versions(metadata.get(name, {}), installed_versions()),
                    "pending_source": revisions.get(name, refs.get(name, "")),
                    "issue": number,
                    "status": "needs-pkgrel",
                }
                sources[name] = revisions.get(name, refs.get(name, ""))
                log_path.write_text(f"{detail}\nPlease bump pkgrel in {row['url']}\n")
                continue
            if dependents[name] & selected:
                run(["pacman", "-U", "--noconfirm", "--asdeps", *map(str, built)])
            for info in built_info:
                old = published.pop(info.get("pkgname", ""), None)
                if old:
                    item = Path(old["_path"])
                    item.unlink(missing_ok=True)
                    item.with_name(item.name + ".sig").unlink(missing_ok=True)
            for item in stage.iterdir():
                shutil.move(str(item), PKGDEST / item.name)
            for info, package in zip(built_info, built):
                info["_path"] = str(PKGDEST / package.name)
                published[info["pkgname"]] = info
            if row["after"]:
                run(["bash", "-lc", row["after"]])
            results[name] = {"status": "built", "detail": ", ".join(p.name for p in built)}
            print(f"[build] succeeded {name}: {results[name]['detail']}")
            sources[name] = revisions.get(name, refs.get(name, ""))
            old_issue = previous.get(name, {}).get("issue")
            previous[name] = {
                "metadata": metadata.get(name, {}),
                "dependency_versions": dependency_versions(metadata.get(name, {}), installed_versions()),
                "status": "built",
                "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            if old_issue:
                run(["gh", "issue", "close", str(old_issue), "--comment", "A package version newer than the published build was successfully published."], check=False)
        except Exception as exc:
            results[name] = {"status": "failed", "detail": str(exc)}
            with log_path.open("a") as log:
                log.write(f"\nBuildbot error: {exc}\n")
            print(f"::error title=Build failed::{name}: {exc}")
            previous[name] = {"metadata": metadata.get(name, {}), "status": "failed"}

    for name, result in results.items():
        if result["status"] in {"source-failed", "source-check-failed"}:
            previous[name] = {"metadata": metadata.get(name, {}), "status": "source-failed"}
        elif result["status"] == "blocked":
            previous[name] = {"metadata": metadata.get(name, {}), "status": "blocked"}
    for name, row in packages.items():
        if name in metadata:
            previous.setdefault(name, {})["metadata"] = metadata[name]

    state = {"sources": sources, "packages": previous, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    emit_summary(results, changed, dependency_changes, selected)
    failed = sum(result["status"] in {"failed", "source-failed", "source-check-failed"} for result in results.values())
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"failed={failed}\npackages_built={sum(r['status'] == 'built' for r in results.values())}\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"::error::{exc}")
        raise SystemExit(1)
