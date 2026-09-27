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

The GitHub Actions buildbot checks the sources in [`packages.tsv`](packages.tsv) every two hours. It pins each checkout
to the revision observed during source discovery, generates metadata from the actual PKGBUILD, and plans builds in
dependency order. Cycles are logged with their full path; already published packages provide bootstrap dependencies.
A failed package blocks its consumers while unrelated packages can still build and publish.

### Avoiding unnecessary work

- A missing cache does **not** mean every published package changed. The bot first compares declared and published
  versions. Matching/newer published versions establish a baseline without compiling or asking for a pkgrel bump.
- Commits with an identical Git tree do not trigger a rebuild. For static versions, a known source change without a
  version bump is reported as `needs-pkgrel` before compilation. Dynamic `pkgver()` recipes can discover a newer version
  during a build. The comparison uses freshly generated metadata, rather than trusting a stale committed `.SRCINFO`.
- Automatic system dependency triggers default to Python major/minor ABI changes. Python patch releases and arbitrary
  compiler/library package updates do not trigger a rebuild. Extend the explicit dependency list if required.
- Local dependency changes propagate conservatively to native packages and build dependencies. An `any` metapackage,
  theme or settings package is not rebuilt just because one of its runtime dependencies changed.
- A pkgrel request waits for a source change or an explicit manual rebuild. An intentional full/manual build that
  reproduces an existing version is `unchanged`; it does not create a false pkgrel request.
- Removed manifest entries are pruned from cached state. Failure, blocked and deferred states are retried; one package
  with missing metadata does not force a complete rebuild of unrelated packages.

Automatic issue creation is **off by default**. Reasons and statuses remain visible in the Actions summary and logs.
To opt in, set repository Actions variables `BUILDBOT_ISSUES=true` (pkgrel requests) and/or
`BUILDBOT_ARCH_ISSUES=true` (upstream update suggestions). The bot reuses open issues and does not recreate a closed
issue for the same revision/version trigger. Arch suggestions apply to LunaOS-maintained GitLab sources, compare
upstream versions rather than distribution-specific epoch/pkgrel values, account for the current source version,
and are grouped by package base. Existing issues are not automatically deleted or closed.

### Failures and publishing

Every package builds in its own staging directory. Archives are validated before installation or promotion, and
`makepkg -i` installation is deferred until version checks have passed. A failed hook/install keeps the old published
archive. Promotion of split packages rolls back on file replacement errors. Build directories are cleaned after each
attempt so kernel/compiler trees cannot accumulate across the whole run.

`sign.sh` and `update-repo.sh` use `PKGDEST` regardless of their caller's working directory. The database is rebuilt
from the actual archives, removing stale split-package entries. Both database archives are signed noninteractively
and verified before replacing the previous databases. GitHub and mirror uploads transfer packages before databases,
and remove obsolete packages only afterwards. Credentials are not passed in lftp command text.

State and machine-readable results are atomically checkpointed after each package. The Actions cache is saved only
after successful publication. Package failures still produce outputs so successfully built packages can be published
before the final step marks the run failed. Infrastructure failures set `fatal=1` and prevent publication.

### Diagnostics

The `lunaos-build-logs-<run_id>-<run_attempt>` artifact contains:

- `buildbot.log`: UTC timestamps, command IDs, arguments, working directories, stdout/stderr, exit codes, durations,
  full exception tracebacks, source revisions, build decisions, dependency cycles and disk availability.
- `<package>.log`: the same events for one package, appended throughout its lifetime; a pkgrel result never overwrites
  earlier compiler output.
- `events.jsonl`: structured events for automated filtering; `plan.json`, `results.json` and `summary.md` describe
  selection and outcomes. A fatal exception additionally produces `fatal.json`.
- Preparation, signing, database generation and upload logs, initial/final resource diagnostics, and the state snapshot.

Commands stream output while running and emit a heartbeat every minute when still active. Stdout and stderr are
separate, so warnings cannot corrupt parsed JSON or revision IDs. Build output is not accumulated in memory. Invalid
UTF-8 is replaced instead of crashing the logger. Secret environment values are redacted from Python logs, and tokens,
passwords and private-key environment variables are removed before running makepkg. Shell diagnostics do not enable
`set -x` or print the environment.

| Variable | Default | Purpose |
|---|---|---|
| `BUILDBOT_COMMAND_TIMEOUT` | `600` seconds | Per-command limit; Git network operations retry up to three times |
| `BUILDBOT_BUILD_TIMEOUT` | `7200` seconds | Maximum duration of a single makepkg invocation |
| `BUILDBOT_RUN_TIMEOUT` | `14400` seconds | Build scheduling budget; remaining packages are deferred to the next run |
| `BUILDBOT_REBUILD_DEPS` | `python` | Comma-separated system dependencies that trigger automatic rebuilds |
| `BUILDBOT_ISSUES` | `false` | Opt in to pkgrel issue creation |
| `BUILDBOT_ARCH_ISSUES` | `false` | Opt in to Arch upstream update issues |
| `BUILDBOT_WORK` | `/tmp/lunaos-buildbot` | Checkout/staging directory |
| `PKGDEST` | `/tmp/lunaos-repo` | Downloaded and newly built package archives |
| `BUILDBOT_LOGDIR` | `/tmp/lunaos-build-logs` | Diagnostic files; overridden to `build-logs` by Actions |
| `BUILDBOT_STATE` | `.build-state/state.json` in this repository | State checkpoint |

Timeouts terminate the entire command process group, including compiler children. The run budget leaves time for
signing, uploading and saving diagnostics before the workflow job limit. Source discovery/preparation and individual
non-build commands retain their own limits. A GitHub/S3 upload is not a single atomic transaction; logs identify which
publication step failed.

To force a rebuild, open **Actions → LunaOS Repo Update → Run workflow** and select **Rebuild every package**, or enter
comma-separated directories in **packages**. These explicit requests override normal version-based skipping.

### Regression tests

```sh
python3 -m unittest discover -s tests -v
```

The unit tests simulate source/network/package failures without installing software or contacting GitHub. On an Arch
host, unprivileged integration tests use real `makepkg`, `fakeroot`, `bsdtar`, `repo-add` and GPG to build tiny local
fixtures, verify signatures and test database replacement. If lftp is present, a local `file://` mirror tests transfer
ordering and cleanup. They use temporary directories and a disposable signing key, never the production repository.
The dedicated `test-buildbot.yml` workflow runs the unit and Arch integration suites on pushes and pull requests.
