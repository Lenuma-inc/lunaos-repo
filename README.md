#  First, install our mirrorlist. 

```sh
sudo pacman -U 'http://lunaos-repo.surge.sh/lunaos-mirrorlist-2-1-x86_64.pkg.tar.zst'
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
