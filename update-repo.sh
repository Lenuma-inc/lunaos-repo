#!/bin/sh
set -eu

: "${repo:?set repo to the database name}"
: "${PKGDEST:?set PKGDEST to the package directory}"
: "${GPG_PASSPHRASE?set GPG_PASSPHRASE (may be empty)}"
case "$repo" in *[!a-zA-Z0-9_-]*|'') echo "invalid repository name" >&2; exit 1 ;; esac
cd "$PKGDEST"
set -- ./*.pkg.tar.zst
[ -e "$1" ] || { echo "no packages to index" >&2; exit 1; }
# Rebuild from the actual archives: updating an old database retains removed
# split packages. Prepare and sign both databases before replacing either.
temporary=$(mktemp -d "$PKGDEST/.database.XXXXXXXX")
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
printf '%s [database] indexing %s archives\n' "$(date -u +%FT%TZ)" "$#"
repo-add --nocolor "$temporary/$repo.db.tar.gz" "$@"
for kind in db files; do
  database="$temporary/$repo.$kind.tar.gz"
  printf '%s\n' "$GPG_PASSPHRASE" | gpg --batch --yes --pinentry-mode loopback --passphrase-fd 0 --detach-sign --output "$database.sig" "$database"
  gpg --batch --verify "$database.sig" "$database"
done
for kind in db files; do
  mv "$temporary/$repo.$kind.tar.gz" "$temporary/$repo.$kind.tar.gz.sig" .
  ln -sfn "$repo.$kind.tar.gz" "$repo.$kind"
  ln -sfn "$repo.$kind.tar.gz.sig" "$repo.$kind.sig"
done
printf '%s [database] ready\n' "$(date -u +%FT%TZ)"
