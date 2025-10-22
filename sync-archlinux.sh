#!/usr/bin/env bash
set -euo pipefail

base_url="https://repository.su/archlinux"
RCLONE_REMOTE="Beget"
RCLONE_PATH="${BEGET_BUCKET}/archlinux-mirror"

echo "🚀 Starting HTTP streaming sync (wget → rclone rcat)..."

upload_file() {
    local dir="$1"
    local file="$2"
    local url="${base_url}/${dir}/${file}"

    echo "→ Uploading ${dir}/${file}"
    if ! curl -fsSL --retry 3 --progress-bar "${url}" | \
        rclone rcat "${RCLONE_REMOTE}:${RCLONE_PATH}/${dir}/${file}" \
        --s3-no-check-bucket --low-level-retries 3 --retries 3 --transfers 1; then
        echo "⚠ Failed to upload: ${dir}/${file}"
        return 0  # продолжаем дальше, не прерываем скрипт
    fi
    echo "✅ Done: ${dir}/${file}"
}

stream_dir() {
    local dir="$1"
    echo "=== Syncing ${dir} ==="

    mapfile -t files < <(wget -qO- "${base_url}/${dir}/" | grep -oE 'href="[^"]+"' | sed -E 's/href="([^"]+)"/\1/' | grep -vE '/$')

    total=${#files[@]}
    echo "Found ${total} files in ${dir}"

    count=0
    for file in "${files[@]}"; do
        ((count++))
        echo "[${count}/${total}] ${file}"
        upload_file "${dir}" "${file}" || true
    done
}

for dir in core/os/x86_64 extra/os/x86_64 multilib/os/x86_64 community/os/x86_64; do
    stream_dir "$dir"
done

echo "✅ HTTP streaming sync completed successfully."
