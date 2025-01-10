#!/bin/bash

log_info() {
    echo -e "\033[1;32m[INFO]\033[0m $1"
}

log_warning() {
    echo -e "\033[1;33m[WARNING]\033[0m $1"
}

log_error() {
    echo -e "\033[1;31m[ERROR]\033[0m $1"
}

log_info "Fetching official Arch Linux binaries from the vendor because building packages manually is just too much work..."

# Fetch the latest version of the Asus Linux G14 kernel and headers
log_info "Retrieving the latest Asus Linux G14 kernel and headers..."
LINUX_G14_VER=$(curl -s https://arch.asus-linux.org/ | grep -oP "linux-g14-\d+\.\d+\.\d+\.arch1-1(?:\.\d+)?-x86_64\.pkg\.tar\.zst" | sort -V | tail -1)
HEADERS_G14_VER=$(curl -s https://arch.asus-linux.org/ | grep -oP "linux-g14-headers-\d+\.\d+\.\d+\.arch1-1(?:\.\d+)?-x86_64\.pkg\.tar\.zst" | sort -V | tail -1)

if [ -z "$LINUX_G14_VER" ] || [ -z "$HEADERS_G14_VER" ]; then
    log_error "Failed to retrieve the Asus Linux G14 kernel or headers versions."
    exit 1
fi

# Fetch the latest ChimeraOS kernel and headers
log_info "Retrieving the latest ChimeraOS kernel and headers..."
LINUX_CHOS_URL=$(curl -s "https://api.github.com/repos/ChimeraOS/linux-chimeraos/releases" \
    | awk '{
        if ($0 ~ /"prerelease": false/) { prerelease = 0; }
        else if ($0 ~ /"prerelease": true/) { prerelease = 1; }
        if (!prerelease && $0 ~ /"browser_download_url":/) {
            match($0, /"browser_download_url": "(https[^"]*)"/, arr);
            print arr[1];
        }
    }' | grep -i "linux-chimeraos" | head -1)

LINUX_CHOS_HEADERS_URL=$(curl -s "https://api.github.com/repos/ChimeraOS/linux-chimeraos/releases" \
    | awk '{
        if ($0 ~ /"prerelease": false/) { prerelease = 0; }
        else if ($0 ~ /"prerelease": true/) { prerelease = 1; }
        if (!prerelease && $0 ~ /"browser_download_url":/) {
            match($0, /"browser_download_url": "(https[^"]*)"/, arr);
            print arr[1];
        }
    }' | grep "linux-chimeraos-headers" | head -1)

if [ -z "$LINUX_CHOS_URL" ] || [ -z "$LINUX_CHOS_HEADERS_URL" ]; then
    log_error "Failed to retrieve ChimeraOS kernel or headers URLs."
    exit 1
fi

# Fetch the Microsoft Surface related packages
log_info "Retrieving Microsoft Surface related packages..."
LINUX_SURFACE_IPTSD_URL=$(sudo pacman -Sp iptsd)
LINUX_SURFACE_KERNEL_URL=$(sudo pacman -Sp linux-surface)
LINUX_SURFACE_KERNEL_HEADERS_URL=$(sudo pacman -Sp linux-surface-headers)
LINUX_SURFACE_ATH10K_FIRMARE_URL=$(sudo pacman -Sp surface-ath10k-firmware-override)
LINUX_SURFACE_IPTS_FIRMARE_URL=$(sudo pacman -Sp surface-ipts-firmware)

if [ -z "$LINUX_SURFACE_IPTSD_URL" ] || [ -z "$LINUX_SURFACE_KERNEL_URL" ] || [ -z "$LINUX_SURFACE_KERNEL_HEADERS_URL" ] || [ -z "$LINUX_SURFACE_ATH10K_FIRMARE_URL" ] || [ -z "$LINUX_SURFACE_IPTS_FIRMARE_URL" ]; then
    log_error "Failed to retrieve one or more Microsoft Surface related package URLs."
    exit 1
fi

# Download Asus Linux kernel and headers
log_info "Downloading Asus Linux kernel ($LINUX_G14_VER) from official Arch binaries..."
if ! wget "https://arch.asus-linux.org/$LINUX_G14_VER"; then
    log_error "Failed to download Asus Linux kernel: $LINUX_G14_VER."
    exit 1
fi
log_info "Kernel download complete."

if ! wget "https://arch.asus-linux.org/$HEADERS_G14_VER"; then
    log_error "Failed to download Asus Linux kernel headers: $HEADERS_G14_VER."
    exit 1
fi
log_info "Headers download complete."

# Download ChimeraOS kernel and headers
log_info "Downloading ChimeraOS kernel for better HDD device support..."
if ! wget $LINUX_CHOS_URL; then
    log_error "Failed to download ChimeraOS kernel."
    exit 1
fi
log_info "ChimeraOS kernel download complete."

if ! wget $LINUX_CHOS_HEADERS_URL; then
    log_error "Failed to download ChimeraOS headers."
    exit 1
fi
log_info "ChimeraOS headers download complete."

# Download Microsoft Surface packages
log_info "Downloading Microsoft Surface support packages from Surface Linux repository..."
if ! wget $LINUX_SURFACE_IPTSD_URL; then
    log_warning "Failed to download Surface iptsd package."
else
    log_info "Surface iptsd package download complete."
fi

if ! wget $LINUX_SURFACE_KERNEL_URL; then
    log_warning "Failed to download Surface kernel package."
else
    log_info "Surface kernel package download complete."
fi

if ! wget $LINUX_SURFACE_KERNEL_HEADERS_URL; then
    log_warning "Failed to download Surface kernel headers package."
else
    log_info "Surface kernel headers package download complete."
fi

if ! wget $LINUX_SURFACE_ATH10K_FIRMARE_URL; then
    log_warning "Failed to download Surface ath10k firmware package."
else
    log_info "Surface ath10k firmware package download complete."
fi

if ! wget $LINUX_SURFACE_IPTS_FIRMARE_URL; then
    log_warning "Failed to download Surface ipts firmware package."
else
    log_info "Surface ipts firmware package download complete."
fi

log_info "All downloads completed with some warnings (check logs)."
