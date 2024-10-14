#!/bin/bash

LINUX_G14_VER=$(curl -s https://arch.asus-linux.org/ | grep -oP "linux-g14-\d+\.\d+\.\d+\.arch1-1(?:\.\d+)?-x86_64\.pkg\.tar\.zst" | sort -V | tail -1)
HEADERS_G14_VER=$(curl -s https://arch.asus-linux.org/ | grep -oP "linux-g14-headers-\d+\.\d+\.\d+\.arch1-1(?:\.\d+)?-x86_64\.pkg\.tar\.zst" | sort -V | tail -1)
LINUX_CHOS_URL=$(curl -s "https://api.github.com/repos/ChimeraOS/linux-chimeraos/releases" \
        | awk '{
    if ($0 ~ /"prerelease": false/) {
        prerelease = 0;
    } else if ($0 ~ /"prerelease": true/) {
        prerelease = 1;
    }
    if (!prerelease && $0 ~ /"browser_download_url":/) {
        match($0, /"browser_download_url": "(https[^"]*)"/, arr);
        print arr[1];
    }
}' | grep -i "linux-chimeraos" | head -1)
LINUX_CHOS_HEADERS_URL=$(curl -s "https://api.github.com/repos/ChimeraOS/linux-chimeraos/releases" \
        | awk '{
    if ($0 ~ /"prerelease": false/) {
        prerelease = 0;
    } else if ($0 ~ /"prerelease": true/) {
        prerelease = 1;
    }
    if (!prerelease && $0 ~ /"browser_download_url":/) {
        match($0, /"browser_download_url": "(https[^"]*)"/, arr);
        print arr[1];
    }
}' | grep "linux-chimeraos-" | head -1)

wget "https://arch.asus-linux.org/$LINUX_G14_VER"
wget "https://arch.asus-linux.org/$HEADERS_G14_VER"
wget $LINUX_CHOS_URL
wget $LINUX_CHOS_HEADERS_URL

