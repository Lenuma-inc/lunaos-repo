#!/usr/bin/env bash
for pkg in *.pkg.tar.zst; do
  echo "Signing package $pkg"
  gpg --detach-sign --pinentry-mode loopback --passphrase ${{ secrets.GPG_PRIVATE_KEY }} --passphrase-fd 0 --output $pkg.sig --sign $pkg
done
