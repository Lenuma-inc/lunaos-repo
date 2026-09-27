#!/bin/sh
set -eu

: "${PKGDEST:?set PKGDEST to the package directory}"
: "${GPG_PASSPHRASE?set GPG_PASSPHRASE (may be empty)}"
cd "$PKGDEST"
set -- ./*.pkg.tar.zst
[ -e "$1" ] || { echo "no packages to sign" >&2; exit 1; }
for pkg do
  printf '%s [sign] %s\n' "$(date -u +%FT%TZ)" "$pkg"
  printf '%s\n' "$GPG_PASSPHRASE" | gpg --batch --yes --pinentry-mode loopback --passphrase-fd 0 --detach-sign --output "$pkg.sig" "$pkg"
  gpg --batch --verify "$pkg.sig" "$pkg"
done
