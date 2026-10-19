#!/bin/bash
set -euo pipefail
export LC_ALL=C

: "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID is required}"
: "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY is required}"
: "${BUCKET_NAME:?BUCKET_NAME is required}"
if [[ ! "$BUCKET_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]]; then
    echo '[ERROR] Invalid bucket name' >&2
    exit 1
fi

MIRROR_BASE='https://mirrors.ocf.berkeley.edu/archlinux'
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
CONFIG

export AWS_CONFIG_FILE="$TEMP_DIR/aws.conf"
export AWS_PAGER=''
cat > "$AWS_CONFIG_FILE" <<'CONFIG'
[default]
region = ru1
retry_mode = standard
max_attempts = 3
request_checksum_calculation = when_required
response_checksum_validation = when_required
s3 =
    addressing_style = path
    max_concurrent_requests = 1
CONFIG

aws_s3() {
    aws --endpoint-url https://s3.ru1.storage.beget.cloud --region ru1 "$@"
}

list_remote() {
    # AWS CLI automatically follows all ListObjectsV2 pages.
    aws_s3 s3api list-objects-v2 --bucket "$BUCKET_NAME" --prefix "$prefix" \
        --delimiter / --output json > "$TEMP_DIR/remote.json"
    python3 - "$prefix" "$TEMP_DIR/remote.json" > "$TEMP_DIR/remote.sizes" <<'PYTHON'
import json
import sys

prefix = sys.argv[1]
with open(sys.argv[2]) as stream:
    for item in json.load(stream).get('Contents', []):
        key = item['Key']
        name = key[len(prefix):]
        if key.startswith(prefix) and name and '/' not in name and not any(c.isspace() for c in name):
            print(item['Size'], name)
PYTHON
    awk '{ print $2 }' "$TEMP_DIR/remote.sizes" > "$TEMP_DIR/remote.names"
}

transfer() {
    local file="$1" staging downloaded=0 mirror size
    staging=$(mktemp -d "$TEMP_DIR/package.XXXXXXXXXX")
    trap "rm -rf -- '$staging'" EXIT
    for mirror in "$MIRROR_BASE" https://frankfurt.mirror.pkgbuild.com https://ftp.halifax.rwth-aachen.de/archlinux; do
        if curl --fail --silent --show-error --location --retry 2 \
            --output "$staging/$file" "$mirror/$repo/os/x86_64/$file"; then
            downloaded=1
            break
        fi
    done
    [[ "$downloaded" == 1 && -s "$staging/$file" ]]
    size=$(awk -v name="$file" '$1 == name { print $2 }' "$TEMP_DIR/expected.sizes")
    [[ "$size" == '?' || "$(stat -c %s "$staging/$file")" == "$size" ]]
    awk -v name="$file" '$2 == name' "$TEMP_DIR/expected.sha256" > "$staging/checksums"
    if [[ -s "$staging/checksums" ]]; then
        (cd "$staging" && sha256sum --check --quiet checksums)
    fi
    aws_s3 s3 cp "$staging/$file" "$target_dir/$file" --only-show-errors
}
export -f aws_s3 transfer
export TEMP_DIR MIRROR_BASE BUCKET_NAME

curl --fail --silent --show-error --location --retry 2 \
    --output "$TEMP_DIR/lastsync" "$MIRROR_BASE/lastsync"
if [[ ! "$(cat "$TEMP_DIR/lastsync")" =~ ^[0-9]+$ ]]; then
    echo '[ERROR] Invalid upstream lastsync' >&2
    exit 1
fi

for repo in core multilib extra; do
    echo "[INFO] Syncing $repo"
    metadata="$TEMP_DIR/$repo"
    mkdir "$metadata"
    source_url="$MIRROR_BASE/$repo/os/x86_64"
    prefix="archlinux-mirror/$repo/os/x86_64/"
    target_dir="s3://$BUCKET_NAME/${prefix%/}"
    export repo target_dir

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
    python3 - "$metadata/$repo.db" "$TEMP_DIR/expected.sha256" > "$TEMP_DIR/expected.sizes" <<'PY'
import base64
import hashlib
import sys
import tarfile

with tarfile.open(sys.argv[1]) as archive, open(sys.argv[2], 'w') as checksums:
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
        checksum = fields['%SHA256SUM%']
        if len(checksum) != 64 or any(c not in '0123456789abcdef' for c in checksum):
            raise ValueError(f'Invalid package checksum: {name}')
        checksums.write(f'{checksum}  {name}\n')
        if signature:
            digest = hashlib.sha256(base64.b64decode(signature, validate=True)).hexdigest()
            checksums.write(f'{digest}  {name}.sig\n')
        print(f'{name}\t{size}')
        print(f'{name}.sig\t{signature_size}')
PY
    list_remote
    awk 'FILENAME == ARGV[1] { if ($1 ~ /^[0-9]+$/) sizes[$2] = $1; next }
         $2 == "?" || !($1 in sizes) || sizes[$1] != $2 { print $1 }' \
        "$TEMP_DIR/remote.sizes" "$TEMP_DIR/expected.sizes" > "$TEMP_DIR/transfer.names"
    echo "[INFO] $repo: $(wc -l < "$TEMP_DIR/transfer.names") of $(wc -l < "$TEMP_DIR/required.names") files need transfer"

    # Each worker stages one verified file; S3 publishes only complete uploads.
    xargs -r -d '\n' -P "$PARALLEL_JOBS" -I '{}' \
        bash -euo pipefail -c 'transfer "$1"' _ '{}' < "$TEMP_DIR/transfer.names"
    list_remote
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
    aws_s3 s3 cp "$metadata/" "$target_dir/" --recursive --only-show-errors
    for database in db files; do
        signature="$repo.$database.sig"
        if [[ ! -f "$metadata/$signature" ]] &&
            grep -Fxq "$signature" "$TEMP_DIR/remote.sorted"; then
            aws_s3 s3 rm "$target_dir/$signature" --only-show-errors
        fi
    done
    while IFS= read -r file; do
        aws_s3 s3 rm "$target_dir/$file" --only-show-errors
    done < "$TEMP_DIR/obsolete.names"
    echo "[OK] $repo synchronized"
done

aws_s3 s3 cp "$TEMP_DIR/lastsync" "s3://$BUCKET_NAME/archlinux-mirror/lastsync" \
    --content-type text/plain --cache-control no-cache --only-show-errors
echo '[OK] Arch mirror synchronization completed'
