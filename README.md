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

The buildbot runs every two hours. It builds changed packages and their dependents,
checks ABI compatibility, and opens issues for upstream updates or required `pkgrel`
bumps. Failed builds block their dependents; other packages are still published.

Sources and build options are listed in [`packages.tsv`](packages.tsv). To rebuild
packages manually, open **Actions → LunaOS Repo Update → Run workflow** and select
all packages or enter package directories.

Removing a source from `packages.tsv` retires its published packages. For older
archives without `.buildbot.json` metadata, also add the `pkgbase` to
[`retired-pkgbases.txt`](retired-pkgbases.txt).

## Arch mirror sync

`arch-sync.yml` syncs Arch packages to Beget S3 at
`https://s3.ru1.storage.beget.cloud` (region `ru1`), under
`archlinux-mirror/{core,multilib,extra}/os/x86_64/`.
After all repositories sync successfully, `archlinux-mirror/lastsync` publishes
the upstream Unix timestamp captured before the sync.

The workflow uses these GitHub Actions secrets:

- `FTP_USERNAME`: S3 access key.
- `FTP_PASSWORD`: S3 secret key.
- `S3_BUCKET_NAME`: bucket name.

For a local run, install `aws-cli`, `curl`, `lftp`, and Python. Set
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `BUCKET_NAME`, then run
`./sync-archlinux.sh`.
