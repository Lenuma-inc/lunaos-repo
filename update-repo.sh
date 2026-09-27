#!/bin/sh
set -eu

: "${repo:?set repo to the database name}"
cd "${PKGDEST:-.}"
set -- ./*.pkg.tar.zst
[ -e "$1" ] || { echo "no packages to index" >&2; exit 1; }
rm -f "$repo.db" "$repo.db.sig" "$repo.db.tar.gz" "$repo.db.tar.gz.sig" \
  "$repo.files" "$repo.files.sig" "$repo.files.tar.gz" "$repo.files.tar.gz.sig"
repo-add --verify --sign "$repo.db.tar.gz" "$@"
