#  First, install our and chaotic-aur mirrorlist. 

```sh
sudo pacman-key --recv-key 3056513887B78AEB --keyserver keyserver.ubuntu.com
sudo pacman-key --lsign-key 3056513887B78AEB
sudo pacman -U 'http://lunaos-repo.surge.sh/lunaos-mirrorlist-2-1-x86_64.pkg.tar.zst' 'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-keyring.pkg.tar.zst' 'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-mirrorlist.pkg.tar.zst'
```

#  Append (adding to the end of the file) to /etc/pacman.conf: 

```sh
[lunaos-repo]
SigLevel = Never
Include = /etc/pacman.d/lunaos-mirrorlist

[chaotic-aur]
Include = /etc/pacman.d/chaotic-mirrorlist
```

# Repository build status
[![LunaOS Repo Update](https://github.com/Boria138/lunaos-repo-actions/actions/workflows/update-lunaos-repo.yml/badge.svg)](https://github.com/Boria138/lunaos-repo-actions/actions/workflows/update-lunaos-repo.yml)
