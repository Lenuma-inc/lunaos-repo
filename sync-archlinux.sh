#!/bin/bash
set -euo pipefail
export LC_ALL=C

: "${FTP_USERNAME:?FTP_USERNAME is required}"
: "${FTP_PASSWORD:?FTP_PASSWORD is required}"
: "${BUCKET_NAME:?BUCKET_NAME is required}"
if [[ ! "$BUCKET_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]]; then
    echo '[ERROR] Invalid bucket name' >&2
    exit 1
fi

MIRROR_BASE='https://mirrors.ocf.berkeley.edu/archlinux'
LFTP_HOST='ftp.ru1.storage.beget.cloud'
PARALLEL_JOBS=5
PACKAGE_PATTERN='^[a-zA-Z0-9][a-zA-Z0-9_@+:.%-]*[.]pkg[.]tar[.](zst|xz)$'
TEMP_DIR=$(mktemp -d -t arch-mirror.XXXXXXXXXX)
trap 'rm -rf -- "$TEMP_DIR"' EXIT
trap 'echo "[ERROR] Mirror sync failed at line $LINENO" >&2' ERR

cat > "$TEMP_DIR/lftp.conf" <<'CONFIG'
set cmd:fail-exit yes
set cmd:move-background no
set net:timeout 30
set net:max-retries 3
set net:reconnect-interval-base 2
set net:reconnect-interval-max 10
set ftp:passive-mode yes
set ftp:ssl-force yes
set ftp:ssl-protect-data yes
set xfer:use-temp-file yes
CONFIG

for repo in core multilib extra; do
    echo "[INFO] Syncing $repo"
    metadata="$TEMP_DIR/$repo"
    mkdir "$metadata"
    source_url="$MIRROR_BASE/$repo/os/x86_64"
    target_dir="$BUCKET_NAME/archlinux-mirror/$repo/os/x86_64"

    # Pin the databases before copying the packages they reference.
    lftp <<EOF
source "$TEMP_DIR/lftp.conf"
mirror --no-recursion --dereference --no-perms --verbose \
    --exclude-glob '*' --include '(^|/)$repo\.(db|files)(\.sig)?$' \
    "$source_url" "$metadata"
bye
EOF
    for database in db files; do
        tar -xOf "$metadata/$repo.$database" --wildcards '*/desc' |
            awk '$0 == "%FILENAME%" { getline; print }' |
            sort -u > "$TEMP_DIR/$database.names"
        if [[ ! -s "$TEMP_DIR/$database.names" ]] ||
            grep -qEv "$PACKAGE_PATTERN" "$TEMP_DIR/$database.names"; then
            echo "[ERROR] Invalid or empty $repo.$database package list" >&2
            exit 1
        fi
    done
    if ! cmp -s "$TEMP_DIR/db.names" "$TEMP_DIR/files.names"; then
        echo "[ERROR] $repo.db and $repo.files describe different packages; retry later" >&2
        exit 1
    fi
    awk '{ print; print $0 ".sig" }' "$TEMP_DIR/db.names" |
        sort -u > "$TEMP_DIR/required.names"

    # Reuse FTP connections and publish each upload only when it is complete.
    lftp -u "$FTP_USERNAME,$FTP_PASSWORD" "$LFTP_HOST" <<EOF
source "$TEMP_DIR/lftp.conf"
mkdir -p "$BUCKET_NAME/archlinux-mirror/$repo/os/x86_64"
mirror --reverse --no-recursion --dereference --no-perms --ignore-time \
    --parallel=$PARALLEL_JOBS --verbose --exclude-glob '*' \
    --include-glob-from="$TEMP_DIR/required.names" "$source_url" "$target_dir"
cd "$BUCKET_NAME/archlinux-mirror/$repo/os/x86_64"
cls -1 > "$TEMP_DIR/remote.names"
bye
EOF
    sort -u "$TEMP_DIR/remote.names" > "$TEMP_DIR/remote.sorted"
    comm -23 "$TEMP_DIR/required.names" "$TEMP_DIR/remote.sorted" > "$TEMP_DIR/missing.names"
    if [[ -s "$TEMP_DIR/missing.names" ]]; then
        echo "[ERROR] $repo is missing packages or signatures; keeping published databases" >&2
        cat "$TEMP_DIR/missing.names" >&2
        exit 1
    fi

    awk -v pattern="${PACKAGE_PATTERN%$}([.]sig)?$" '$0 ~ pattern' \
        "$TEMP_DIR/remote.sorted" > "$TEMP_DIR/remote.packages"
    comm -13 "$TEMP_DIR/required.names" "$TEMP_DIR/remote.packages" > "$TEMP_DIR/obsolete.names"
    {
        echo "source \"$TEMP_DIR/lftp.conf\""
        echo "mirror --reverse --no-recursion --no-perms --transfer-all --verbose \"$metadata\" \"$target_dir\""
        echo "cd \"$BUCKET_NAME/archlinux-mirror/$repo/os/x86_64\""
        for database in db files; do
            signature="$repo.$database.sig"
            if [[ ! -f "$metadata/$signature" ]] &&
                grep -Fxq "$signature" "$TEMP_DIR/remote.sorted"; then
                printf 'rm "%s"\n' "$signature"
            fi
        done
        while IFS= read -r file; do
            printf 'rm "%s"\n' "$file"
        done < "$TEMP_DIR/obsolete.names"
        echo bye
    } > "$TEMP_DIR/publish.lftp"
    lftp -u "$FTP_USERNAME,$FTP_PASSWORD" "$LFTP_HOST" < "$TEMP_DIR/publish.lftp"
    echo "[OK] $repo synchronized"
done

echo '[OK] Arch mirror synchronization completed'
