#!/bin/bash
set -euo pipefail
: "${PKGDEST:?set PKGDEST}"
: "${repo:?set repo}"
cd "$PKGDEST"
shopt -s nullglob
packages=(*.pkg.tar.zst *.pkg.tar.zst.sig)
databases=("$repo.db" "$repo.db.sig" "$repo.db.tar.gz" "$repo.db.tar.gz.sig"
           "$repo.files" "$repo.files.sig" "$repo.files.tar.gz" "$repo.files.tar.gz.sig")
((${#packages[@]})) || { echo "No packages to publish" >&2; exit 1; }
for asset in "${databases[@]}"; do
  [[ -s "$asset" ]] || { echo "Missing database asset: $asset" >&2; exit 1; }
done
# The release is verified/created during preparation. Never interpret a failed
# network/authentication check here as an absent release.
gh release view "$repo" >/dev/null
for ((offset=0; offset<${#packages[@]}; offset+=20)); do
  printf '%s [publish] package assets %s/%s\n' "$(date -u +%FT%TZ)" "$offset" "${#packages[@]}"
  gh release upload "$repo" "${packages[@]:offset:20}" --clobber
done
# Clients keep using the old database until all referenced packages exist.
gh release upload "$repo" "${databases[@]}" --clobber
assets=$(gh release view "$repo" --json assets --jq '.assets[].name')
while IFS= read -r asset; do
  case "$asset" in
    *.pkg.tar.zst|*.pkg.tar.zst.sig)
      if [[ ! -e "$asset" ]]; then
        printf '%s [publish] remove obsolete %s\n' "$(date -u +%FT%TZ)" "$asset"
        gh release delete-asset "$repo" "$asset" --yes
      fi ;;
  esac
done <<< "$assets"
