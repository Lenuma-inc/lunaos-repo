#!/bin/sh
set -eu

: "${GPG_PASSPHRASE:?set GPG_PASSPHRASE}"
cd "${PKGDEST:-.}"
set -- ./*.pkg.tar.zst
[ -e "$1" ] || { echo "no packages to sign" >&2; exit 1; }
for pkg do
  if [ -f "$pkg.sig" ] && gpg --batch --verify "$pkg.sig" "$pkg"; then
    continue
  fi
  printf '%s\n' "$GPG_PASSPHRASE" | gpg --batch --yes --pinentry-mode loopback --passphrase-fd 0 --detach-sign --output "$pkg.sig" "$pkg"
done
