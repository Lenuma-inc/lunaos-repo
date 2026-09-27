#!/usr/bin/env python3
"""Publish verified repository files without replacing unchanged packages."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time


SAFE_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._+@:%-]*")


def repository_files(root: Path, repo: str) -> dict[str, str]:
    if not SAFE_NAME.fullmatch(repo):
        raise ValueError("Unsafe repository name")
    hashes = {}
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        if not SAFE_NAME.fullmatch(path.name) or path.resolve().parent != root.resolve():
            raise ValueError(f"Unsafe repository file: {path.name}")
        with path.open("rb") as stream:
            hashes[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    for suffix in ("db", "files"):
        if f"{repo}.{suffix}.sig" not in hashes:
            raise ValueError(f"Missing database signature: {repo}.{suffix}.sig")
    indexed = database_packages(root / f"{repo}.db")
    if database_packages(root / f"{repo}.files") != indexed:
        raise ValueError("Package and file databases do not match")
    for name, digest in indexed.items():
        if hashes.get(name) != digest or name + ".sig" not in hashes:
            raise ValueError(f"Missing or mismatched package/signature: {name}")
    packages = {name for name in hashes if name.endswith(".pkg.tar.zst")}
    if not indexed or set(indexed) != packages:
        raise ValueError("Repository database does not match the package set")
    for name in hashes:
        for suffix in (".sig", ".buildbot.json"):
            if name.endswith(".pkg.tar.zst" + suffix) and name[:-len(suffix)] not in indexed:
                raise ValueError(f"Orphaned package file: {name}")
    return hashes


def database_packages(path: Path) -> dict[str, str]:
    packages = {}
    with tarfile.open(path) as archive:
        for entry in archive.getmembers():
            if not entry.name.endswith("/desc") or not entry.isfile():
                continue
            stream = archive.extractfile(entry)
            if stream is None:
                raise ValueError("Unreadable database entry")
            fields = stream.read().decode().splitlines()
            try:
                name = fields[fields.index("%FILENAME%") + 1]
                digest = fields[fields.index("%SHA256SUM%") + 1]
            except (ValueError, IndexError) as exc:
                raise ValueError("Incomplete repository database entry") from exc
            if (not name.endswith(".pkg.tar.zst") or not SAFE_NAME.fullmatch(name) or
                    not re.fullmatch(r"[a-f0-9]{64}", digest) or name in packages):
                raise ValueError("Invalid repository database package")
            packages[name] = digest
    return packages


def release_assets(repository: str, release_id: int) -> dict[str, dict]:
    result = subprocess.run(["gh", "api", "--paginate", "--jq", ".[] | [.id, .name, .digest] | @tsv",
                             f"repos/{repository}/releases/{release_id}/assets"],
                            check=True, text=True, capture_output=True)
    assets = {}
    for line in result.stdout.splitlines():
        asset_id, name, digest = line.split("\t")
        if not SAFE_NAME.fullmatch(name):
            raise ValueError("Unsafe release asset name")
        assets[name] = {"id": int(asset_id), "digest": digest}
    return assets


def delete_release_asset(repository: str, asset_id: int) -> None:
    for attempt in range(3):
        try:
            subprocess.run(["gh", "api", "--method", "DELETE",
                            f"repos/{repository}/releases/assets/{asset_id}"], check=True)
            return
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def publish_release(root: Path, repo: str, hashes: dict[str, str], metadata: set[str]) -> None:
    repository = os.environ["GH_REPO"]
    endpoint = f"repos/{repository}/releases/tags/{repo}"
    result = subprocess.run(["gh", "api", endpoint], text=True, capture_output=True)
    if result.returncode:
        if "HTTP 404" not in result.stderr:
            raise RuntimeError("Could not read the release; refusing to replace it")
        subprocess.run(["gh", "release", "create", repo, "--title", "LunaOS package repository",
                        "--notes", "Updated package repository"], check=True)
        result = subprocess.run(["gh", "api", endpoint], check=True, text=True, capture_output=True)
    release_id = json.loads(result.stdout)["id"]
    assets = release_assets(repository, release_id)
    changed = {name for name, digest in hashes.items()
               if assets.get(name, {}).get("digest") != "sha256:" + digest}
    for names, required in ((changed - metadata, hashes.keys() - metadata),
                            (changed & metadata, hashes.keys())):
        if names:
            subprocess.run(["gh", "release", "upload", repo, "--clobber",
                            *(str(root / name) for name in sorted(names))], check=True)
        current = release_assets(repository, release_id)
        for name in required:
            if current.get(name, {}).get("digest") != "sha256:" + hashes[name]:
                raise RuntimeError(f"Release verification failed: {name}")
    for name, asset in assets.items():
        if name not in hashes:
            delete_release_asset(repository, asset["id"])
    print(f"[OK] Release: {len(changed)} changed files uploaded")


def publish_s3(root: Path, repo: str, hashes: dict[str, str], metadata: set[str]) -> None:
    server = os.environ["FTP_SERVER"]
    target = os.environ["SERVER_DIR"].rstrip("/")
    if (not re.fullmatch(r"[a-zA-Z0-9.-]+", server) or
            any(not SAFE_NAME.fullmatch(part) for part in target.split("/"))):
        raise ValueError("Unsafe FTP destination")
    connection = ["lftp", "-u", os.environ["FTP_USERNAME"] + "," + os.environ["FTP_PASSWORD"], server]
    settings = ("set cmd:fail-exit yes\nset cmd:move-background no\nset ftp:ssl-force yes\n"
                "set ftp:ssl-protect-data yes\nset ssl:check-hostname yes\nset ssl:verify-certificate yes\n"
                "set net:timeout 30\nset net:max-retries 3\n"
                "set xfer:use-temp-file yes\n")
    catalog = f"{repo}.sha256.json"
    with tempfile.TemporaryDirectory(prefix="lunaos-publish-") as temp:
        staging = Path(temp)
        previous = staging / "previous.json"
        listing = staging / "remote.names"
        commands = (settings + f'mkdir -p "{target}"\ncd "{target}"\n'
                    f'get "{catalog}" -o "{previous}" || echo "No usable upload catalog; checking all files"\n'
                    f'cls -1 > "{listing}"\nbye\n')
        subprocess.run(connection, input=commands, text=True, check=True)
        old_hashes = {}
        if previous.exists():
            try:
                old_hashes = json.loads(previous.read_text())
                if not isinstance(old_hashes, dict):
                    raise ValueError("Expected a hash mapping")
            except ValueError:
                print("[WARN] Invalid upload catalog; uploading all files")
                old_hashes = {}
        present = set(listing.read_text().splitlines())
        changed = {name for name, digest in hashes.items() if old_hashes.get(name) != digest or name not in present}
        for phase, required in (("packages", hashes.keys() - metadata), ("databases", hashes.keys())):
            directory = staging / phase
            directory.mkdir()
            for name in changed & (metadata if phase == "databases" else hashes.keys() - metadata):
                (directory / name).symlink_to((root / name).resolve())
            commands = settings
            if any(directory.iterdir()):
                commands += (f'mirror --reverse --no-recursion --no-perms --dereference --transfer-all '
                             f'--parallel={1 if phase == "databases" else 4} --verbose "{directory}" "{target}"\n')
            commands += f'cd "{target}"\ncls -1 > "{listing}"\nbye\n'
            subprocess.run(connection, input=commands, text=True, check=True)
            present = set(listing.read_text().splitlines())
            if missing := required - present:
                raise RuntimeError("FTP verification failed: " + ", ".join(sorted(missing)))
        local_catalog = staging / catalog
        local_catalog.write_text(json.dumps(hashes, sort_keys=True) + "\n")
        commands = settings + f'cd "{target}"\n'
        for name in sorted(present - hashes.keys() - {catalog}):
            if name.startswith("."):
                continue
            if not SAFE_NAME.fullmatch(name):
                raise ValueError("Unsafe remote filename; refusing cleanup")
            commands += f'rm "{name}"\n'
        commands += f'put "{local_catalog}" -o "{catalog}"\nbye\n'
        subprocess.run(connection, input=commands, text=True, check=True)
        print(f"[OK] FTP: {len(changed)} changed files uploaded")


def main() -> None:
    root = Path(os.environ["PKGDEST"])
    repo = os.environ["repo"]
    hashes = repository_files(root, repo)
    metadata = {f"{repo}.{suffix}" for suffix in ("db", "db.sig", "db.tar.gz", "db.tar.gz.sig",
                                                 "files", "files.sig", "files.tar.gz", "files.tar.gz.sig")}
    if sys.argv[1:] == ["release"]:
        publish_release(root, repo, hashes, metadata)
    elif sys.argv[1:] == ["s3"]:
        publish_s3(root, repo, hashes, metadata)
    else:
        raise ValueError("Expected release or s3")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Publication command failed with exit code {exc.returncode}", file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, RuntimeError, KeyError, tarfile.TarError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
