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

    # HTTP directory listings contain rounded sizes; the database has exact byte counts.
    python3 - "$metadata/$repo.db" > "$TEMP_DIR/expected.sizes" <<'PY'
import base64
import sys
import tarfile

with tarfile.open(sys.argv[1]) as archive:
    for entry in archive.getmembers():
        if not entry.isfile() or not entry.name.endswith('/desc'):
            continue
        with archive.extractfile(entry) as stream:
            fields = {}
            for block in stream.read().decode().strip().split('\n\n'):
                key, _, value = block.partition('\n')
                fields[key] = value
        name = fields['%FILENAME%']
        size = int(fields['%CSIZE%'])
        if size <= 0:
            raise ValueError(f'Invalid package size: {name}')
        signature = fields.get('%PGPSIG%')
        signature_size = len(base64.b64decode(signature, validate=True)) if signature else '?'
        print(f'{name}\t{size}')
        print(f'{name}.sig\t{signature_size}')
PY
    lftp -u "$FTP_USERNAME,$FTP_PASSWORD" "$LFTP_HOST" <<EOF
source "$TEMP_DIR/lftp.conf"
mkdir -p "$target_dir"
cd "$target_dir"
cls -1 --size --block-size=1 > "$TEMP_DIR/remote.sizes"
bye
EOF
    awk 'FILENAME == ARGV[1] { if ($1 ~ /^[0-9]+$/) sizes[$2] = $1; next }
         $2 == "?" || !($1 in sizes) || sizes[$1] != $2 { print $1 }' \
        "$TEMP_DIR/remote.sizes" "$TEMP_DIR/expected.sizes" > "$TEMP_DIR/transfer.names"
    echo "[INFO] $repo: $(wc -l < "$TEMP_DIR/transfer.names") of $(wc -l < "$TEMP_DIR/required.names") files need transfer"

    # Reuse FTP connections and publish each upload only when it is complete.
    for attempt in 1 2 3; do
        if lftp -u "$FTP_USERNAME,$FTP_PASSWORD" "$LFTP_HOST" 2> "$TEMP_DIR/transfer.error" <<EOF
source "$TEMP_DIR/lftp.conf"
mkdir -p "$BUCKET_NAME/archlinux-mirror/$repo/os/x86_64"
mirror --reverse --no-recursion --dereference --no-perms --transfer-all \
    --parallel=$PARALLEL_JOBS --verbose --exclude-glob '*' \
    --include-glob-from="$TEMP_DIR/transfer.names" "$source_url" "$target_dir"
cd "$BUCKET_NAME/archlinux-mirror/$repo/os/x86_64"
cache flush
cls -1 > "$TEMP_DIR/remote.names"
cls -1 --size --block-size=1 > "$TEMP_DIR/remote.sizes"
bye
EOF
        then
            cat "$TEMP_DIR/transfer.error" >&2
            break
        else
            status=$?
            cat "$TEMP_DIR/transfer.error" >&2
            # lftp treats this server-side network failure as a permanent FTP error.
            if (( attempt == 3 )) ||
                ! grep -Eq '550 .*connection reset by peer' "$TEMP_DIR/transfer.error"; then
                exit "$status"
            fi
            echo "[WARN] $repo: connection reset; retrying transfer ($attempt/3)" >&2
            sleep 2
        fi
    done
    sort -u "$TEMP_DIR/remote.names" > "$TEMP_DIR/remote.sorted"
    comm -23 "$TEMP_DIR/required.names" "$TEMP_DIR/remote.sorted" > "$TEMP_DIR/missing.names"
    if [[ -s "$TEMP_DIR/missing.names" ]]; then
        echo "[ERROR] $repo is missing packages or signatures; keeping published databases" >&2
        cat "$TEMP_DIR/missing.names" >&2
        exit 1
    fi
    awk 'FILENAME == ARGV[1] { if ($1 ~ /^[0-9]+$/) sizes[$2] = $1; next }
         !($1 in sizes) || sizes[$1] <= 0 || ($2 != "?" && sizes[$1] != $2) { print $1 }' \
        "$TEMP_DIR/remote.sizes" "$TEMP_DIR/expected.sizes" > "$TEMP_DIR/incorrect.sizes"
    if [[ -s "$TEMP_DIR/incorrect.sizes" ]]; then
        echo "[ERROR] $repo has incomplete packages or signatures; keeping published databases" >&2
        cat "$TEMP_DIR/incorrect.sizes" >&2
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
