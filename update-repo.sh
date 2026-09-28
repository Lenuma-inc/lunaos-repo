#!/bin/sh
set -eu

: "${repo:?set repo to the database name}"
cd "${PKGDEST:-.}"
set -- ./*.pkg.tar.zst
[ -e "$1" ] || { echo "no packages to index" >&2; exit 1; }
repo-add --verify --sign "$repo.db.tar.gz" "$@"
