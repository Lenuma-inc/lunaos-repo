#!/bin/bash
set -euo pipefail
: "${GITHUB_WORKSPACE:?set GITHUB_WORKSPACE}"
: "${PKGDEST:?set PKGDEST}"
: "${repo:?set repo}"
prepare_error() {
  local code=$1 line=$2
  printf '%s [prepare] failed at line %s (exit %s)\n' "$(date -u +%FT%TZ)" "$line" "$code" >&2
  exit "$code"
}
trap 'prepare_error "$?" "$LINENO"' ERR
pacman -Syu --noconfirm --disable-download-timeout --needed git curl wget gnupg lftp github-cli python sudo
cp "$GITHUB_WORKSPACE/makepkg.conf" /etc/makepkg.conf
pacman -Scc --noconfirm
pacman-key --init
pacman-key --populate archlinux

# Read from the named package release, not /releases/latest (which can be another release).
mkdir -p "$PKGDEST"
gh release view "$repo" >/dev/null
gh release download "$repo" --dir "$PKGDEST" --pattern '*.pkg.tar.zst' --pattern '*.pkg.tar.zst.sig'
shopt -s nullglob
keyrings=("$PKGDEST"/lunaos-keyring-*.pkg.tar.zst)
mirrorlists=("$PKGDEST"/lunaos-mirrorlist-*.pkg.tar.zst)
if ((${#keyrings[@]} != 1 || ${#mirrorlists[@]} != 1)); then
  echo 'The release must contain exactly one LunaOS keyring and mirrorlist archive' >&2
  exit 1
fi
pacman-key --recv-key 3056513887B78AEB 78B2BAAB82C8D511 --keyserver keyserver.ubuntu.com
pacman-key --lsign-key 3056513887B78AEB 78B2BAAB82C8D511
pacman -U --noconfirm 'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-keyring.pkg.tar.zst'
pacman -U --noconfirm 'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-mirrorlist.pkg.tar.zst'
pacman -U --noconfirm "${keyrings[0]}" "${mirrorlists[0]}"
pacman-key --populate
cat <<'CONF' >> /etc/pacman.conf
[multilib]
Include = /etc/pacman.d/mirrorlist

[lunaos-repo]
Include = /etc/pacman.d/lunaos-mirrorlist
SigLevel = Never

[chaotic-aur]
Include = /etc/pacman.d/chaotic-mirrorlist
SigLevel = Never
CONF
pacman -Syy --noconfirm
useradd -m user -G wheel
printf 'user ALL=(ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/buildbot
chmod 440 /etc/sudoers.d/buildbot
printf 'PACKAGER="LunaOS Team"\nGPGKEY="78B2BAAB82C8D511"\n' >> /etc/makepkg.conf
install -d -o user -g user -m 700 /home/user/.gnupg
printf 'auto-key-retrieve\nkeyserver hkps://keyserver.ubuntu.com\n' > /home/user/.gnupg/gpg.conf
chown user:user /home/user/.gnupg/gpg.conf
runuser -u user -- git config --global user.email 'buildbot@lunaos.local'
runuser -u user -- git config --global user.name 'LunaOS Buildbot'
printf '%s [prepare] container ready\n' "$(date -u +%FT%TZ)"
df -h "$PKGDEST"
