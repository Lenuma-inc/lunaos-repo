# LunaOS package repository

## Install

Install the LunaOS and Chaotic-AUR signing keys and mirror lists:

```sh
sudo pacman-key --recv-key 3056513887B78AEB 78B2BAAB82C8D511 --keyserver keyserver.ubuntu.com
sudo pacman-key --lsign-key 3056513887B78AEB 78B2BAAB82C8D511
sudo pacman -U \
  'https://github.com/lenuma-inc/lunaos-repo/releases/download/lunaos-repo/lunaos-keyring-4-1-any.pkg.tar.zst' \
  'https://github.com/lenuma-inc/lunaos-repo/releases/download/lunaos-repo/lunaos-mirrorlist-2-3-x86_64.pkg.tar.zst' \
  'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-keyring.pkg.tar.zst' \
  'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-mirrorlist.pkg.tar.zst'
```

Add these entries to `/etc/pacman.conf`:

```ini
[lunaos-repo]
Include = /etc/pacman.d/lunaos-mirrorlist

[chaotic-aur]
Include = /etc/pacman.d/chaotic-mirrorlist
```

Then refresh package databases with `sudo pacman -Syu`.

## Build status

[![Repository update](https://github.com/Lenuma-inc/lunaos-repo/actions/workflows/update-lunaos-repo.yml/badge.svg)](https://github.com/Lenuma-inc/lunaos-repo/actions/workflows/update-lunaos-repo.yml)

The buildbot checks package sources every two hours. When a source or build dependency changes, it builds the affected packages and their dependents.

Builds are isolated: one failure does not stop unrelated packages. Packages that need a failed build are reported as blocked; successful packages are still published. The previous published package stays available until its replacement is ready.

The bot also checks for newer Arch and upstream versions and opens issues when a package needs updating or rebuilding. It does not silently publish a rebuild with the same or a lower package version; it asks for a `pkgrel` bump instead.

Publication verifies package SHA256 values against both repository databases, preserves valid signatures, and uploads packages before databases. Unchanged GitHub assets are reused by their SHA256 digest. S3 uses `lunaos-repo.sha256.json` to record the last successful upload and checks that named files still exist; the first upload without this catalog transfers all files. It does not re-download unchanged S3 files to detect external corruption. Obsolete remote files are removed only after the new package and database files are present.

Open a workflow run to see package results and logs. To start one manually, go to **Actions → LunaOS Repo Update → Run workflow**. Choose **Rebuild every package**, or list package directories to rebuild those packages and their dependents. Package sources and build options are listed in [`packages.tsv`](packages.tsv).

Removing a source from `packages.tsv` also removes its published packages when their `.buildbot.json` records that source. For older archives without these sidecars, add their `pkgbase` to [`retired-pkgbases.txt`](retired-pkgbases.txt); this includes split packages. Active package names and bases remain protected from removal.
