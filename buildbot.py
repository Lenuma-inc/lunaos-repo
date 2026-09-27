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
    run(["chown", "-R", "user:user", str(path)])
    return path, run(["git", "rev-parse", "HEAD"], cwd=path)


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
    provider = {name: name for name in packages}
    for directory, info in metadata.items():
        for name in info.get("names", []) + info.get("provides", []):
            provider[name] = directory
    dependencies = {directory: set() for directory in packages}
    dependents = {directory: set() for directory in packages}
    for directory, info in metadata.items():
        for name in info.get("deps", []):
            dependency = provider.get(name)
            if dependency and dependency != directory:
                dependencies[directory].add(dependency)
                dependents[dependency].add(directory)
    return dependencies, dependents


def archive_name(path):
    info = run(["bsdtar", "-xOf", str(path), ".PKGINFO"], check=False)
    return next((line.partition(" = ")[2] for line in info.splitlines() if line.startswith("pkgname = ")), "")


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


def order(selected, dependencies):
    result, remaining = [], set(selected)
    while remaining:
        ready = sorted(p for p in remaining if not (dependencies[p] & remaining))
        if not ready:
            raise RuntimeError("dependency cycle: " + ", ".join(sorted(remaining)))
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

    for name, row in packages.items():
        try:
            refs[name] = source_sha(row)
            if refs[name] != sources.get(name):
                changed.add(name)
        except Exception as exc:
            results[name] = {"status": "source-check-failed", "detail": str(exc)}
            (LOGDIR / f"{name}.log").write_text(f"source check failed: {exc}\n")

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
            path, revision = clone(packages[name])
            revisions[name] = revision
            metadata[name] = package_info(path)
        except Exception as exc:
            results[name] = {"status": "source-failed", "detail": str(exc)}
            (LOGDIR / f"{name}.log").write_text(f"source checkout failed: {exc}\n")
            changed.add(name)

    dependencies, dependents = graph(packages, metadata)
    installed = installed_versions()
    dependency_changes = set()
    for name, info in metadata.items():
        old_versions = previous.get(name, {}).get("dependency_versions", {})
        current_versions = dependency_versions(info, installed)
        if any(dep in old_versions and old_versions[dep] != version for dep, version in current_versions.items()):
            dependency_changes.add(name)
    retry = {name for name, info in previous.items() if info.get("status") in {"failed", "blocked", "source-failed", "source-check-failed"}}
    seeds = set(packages) if full else changed | dependency_changes | requested | retry
    selected = closure(seeds, dependents)
    for name in packages:
        if name in results and name not in selected:
            selected.add(name)
    build_order = order(selected, dependencies)
    firmware = Path(os.environ.get("FIRMWARE_TARBALL", "/tmp/lunaos-input/firmware.tar"))

    for name in build_order:
        if name in results and results[name]["status"] in {"source-failed", "source-check-failed"}:
            continue
        blocked = sorted(dep for dep in dependencies[name] if dep in results and results[dep]["status"] != "built")
        if blocked:
            results[name] = {"status": "blocked", "detail": "failed dependencies: " + ", ".join(blocked)}
            (LOGDIR / f"{name}.log").write_text(results[name]["detail"] + "\n")
            continue
        row = packages[name]
        path = WORK / name
        stage = WORK / "packages" / name
        log_path = LOGDIR / f"{name}.log"
        try:
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
                code = proc.wait()
            if code:
                raise RuntimeError(f"makepkg exited {code}; see {log_path}")
            built = list(stage.glob("*.pkg.tar.zst"))
            if not built:
                raise RuntimeError("makepkg produced no package archives")
            if dependents[name] & selected:
                run(["pacman", "-U", "--noconfirm", "--asdeps", *map(str, built)])
            package_names = set(metadata[name].get("names", []))
            for item in PKGDEST.glob("*.pkg.tar.zst"):
                if archive_name(item) in package_names | {f"{pkgname}-debug" for pkgname in package_names}:
                    item.unlink()
                    item.with_name(item.name + ".sig").unlink(missing_ok=True)
            for item in stage.iterdir():
                shutil.move(str(item), PKGDEST / item.name)
            if row["after"]:
                run(["bash", "-lc", row["after"]])
            results[name] = {"status": "built", "detail": ", ".join(p.name for p in built)}
            sources[name] = revisions.get(name, refs.get(name, ""))
            previous[name] = {
                "metadata": metadata.get(name, {}),
                "dependency_versions": dependency_versions(metadata.get(name, {}), installed_versions()),
                "status": "built",
                "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        except Exception as exc:
            results[name] = {"status": "failed", "detail": str(exc)}
            with log_path.open("a") as log:
                log.write(f"\nBuildbot error: {exc}\n")
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
    failed = sum(result["status"] != "built" for result in results.values())
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"failed={failed}\npackages_built={sum(r['status'] == 'built' for r in results.values())}\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"::error::{exc}")
        raise SystemExit(1)
