#  First, install our and chaotic-aur mirrorlist and keys.

```sh
sudo pacman-key --recv-key 3056513887B78AEB 78B2BAAB82C8D511 --keyserver keyserver.ubuntu.com
sudo pacman-key --lsign-key 3056513887B78AEB 78B2BAAB82C8D511
sudo pacman -U 'https://github.com/lenuma-inc/lunaos-repo/releases/download/lunaos-repo/lunaos-keyring-4-1-any.pkg.tar.zst' 'https://github.com/lenuma-inc/lunaos-repo/releases/download/lunaos-repo/lunaos-mirrorlist-2-3-x86_64.pkg.tar.zst' 'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-keyring.pkg.tar.zst' 'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-mirrorlist.pkg.tar.zst'
```

#  Append (adding to the end of the file) to /etc/pacman.conf: 

```sh
[lunaos-repo]
Include = /etc/pacman.d/lunaos-mirrorlist

[chaotic-aur]
Include = /etc/pacman.d/chaotic-mirrorlist
```

# Repository build status
[![LunaOS Repo Update](https://github.com/Lenuma-inc/lunaos-repo/actions/workflows/update-lunaos-repo.yml/badge.svg)](https://github.com/Lenuma-inc/lunaos-repo/actions/workflows/update-lunaos-repo.yml)

## Buildbot

The GitHub Actions buildbot checks the package source repositories every two hours. Package sources and makepkg options
live in [`packages.tsv`](packages.tsv). It compares upstream revisions with the last successfully published build,
reads each package's `.SRCINFO`, and builds changed packages plus their reverse dependency tree in dependency order.
It also tracks installed build-dependency versions: Python extensions rebuild when Python's minor ABI changes, while
Python bugfix releases alone do not trigger a rebuild.

Each package builds into its own staging directory. A failed package does not stop unrelated builds, its previous
published package remains available, and packages that depend on a failed build are marked blocked. Successful packages
are signed and published even when another package fails. Failed and blocked packages are retried on the next run.

The Actions run summary lists package statuses. Each package's full build output is attached as the
`lunaos-build-logs-*` artifact. The builder also stores source revisions and package status in the Actions cache so it
can select only changed packages between runs.

To request a rebuild, open **Actions → LunaOS Repo Update → Run workflow**. Select **Rebuild every package** for a full
build, or enter comma-separated package directories in **packages** to rebuild those packages and their dependents.
