#!/bin/bash

LINUX_VER=$(curl -s https://arch.asus-linux.org/ | grep -oP "linux-g14-\d+\.\d+\.\d+\.arch1-1(?:\.\d+)?-x86_64\.pkg\.tar\.zst" | sort -V | tail -1)
HEADERS_VER=$(curl -s https://arch.asus-linux.org/ | grep -oP "linux-g14-headers-\d+\.\d+\.\d+\.arch1-1(?:\.\d+)?-x86_64\.pkg\.tar\.zst" | sort -V | tail -1)

curl -LO "https://arch.asus-linux.org/$LINUX_VER"
curl -LO "https://arch.asus-linux.org/$HEADERS_VER"