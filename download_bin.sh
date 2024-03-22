#!/bin/bash

LINUX_G14_VER=$(curl -s https://arch.asus-linux.org/ | grep -oP "linux-g14-\d+\.\d+\.\d+\.arch1-1(?:\.\d+)?-x86_64\.pkg\.tar\.zst" | sort -V | tail -1)
HEADERS_G14_VER=$(curl -s https://arch.asus-linux.org/ | grep -oP "linux-g14-headers-\d+\.\d+\.\d+\.arch1-1(?:\.\d+)?-x86_64\.pkg\.tar\.zst" | sort -V | tail -1)
LINUX_HANDHELD_URL=$(curl -s "https://api.github.com/repos/hhd-dev/linux-handheld/releases/latest" | grep "browser_download_url" | cut -d '"' -f 4)

wget "https://arch.asus-linux.org/$LINUX_G14_VER"
wget "https://arch.asus-linux.org/$HEADERS_G14_VER"
wget $LINUX_HANDHELD_URL
