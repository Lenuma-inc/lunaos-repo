#!/bin/bash
set -o pipefail

REPOS=("core" "multilib" "extra")
ARCH="x86_64"
MIRROR_BASE="https://mirrors.ocf.berkeley.edu/archlinux"

# FTP
LFTP_HOST="ftp.ru1.storage.beget.cloud"
LFTP_USER="$FTP_USERNAME"
LFTP_PASS="$FTP_PASSWORD"
REMOTE_DIR="$BUCKET_NAME/archlinux-mirror"

PARALLEL_JOBS=5
MAX_RETRIES=3

# Create temp directory for downloads
TEMP_DIR=$(mktemp -d -t arch-mirror.XXXXXXXXXX)
trap "rm -rf '$TEMP_DIR'" EXIT

# URL decode function
urldecode() {
    local url_encoded="${1//+/ }"
    printf '%b' "${url_encoded//%/\\x}"
}

# Get list of files from source mirror
get_file_list() {
    local repo=$1
    local mirror="$MIRROR_BASE/$repo/os/$ARCH/"
    echo "[INFO] Fetching file list from $mirror ..." >&2

    local temp_html="$TEMP_DIR/${repo}_source.html"

    if ! curl -s -f --connect-timeout 30 --max-time 60 "$mirror" -o "$temp_html"; then
        echo "[ERROR] Failed to fetch source page: $mirror" >&2
        return 1
    fi

    # Parse links from OCF-style HTML table
    grep -oP '(?<=<td class="link"><a href=")[^"]+' "$temp_html" \
        | grep -E '\.pkg\.tar\.zst$|\.sig$|\.db$|\.files$' \
        | while read -r file; do
            decoded_file=$(urldecode "$file")
            echo "${mirror}${file}|$repo/os/$ARCH/${decoded_file}"
        done > "$TEMP_DIR/${repo}_parsed.txt"

    # Fallback parser if main failed
    if [ ! -s "$TEMP_DIR/${repo}_parsed.txt" ]; then
        echo "[WARN] Fallback parser triggered for $repo" >&2
        grep -oP 'href="[^"]+\.(pkg\.tar\.zst|sig|db|files)"' "$temp_html" \
            | sed 's/^href="//;s/"$//' \
            | while read -r file; do
                decoded_file=$(urldecode "$file")
                echo "${mirror}${file}|$repo/os/$ARCH/${decoded_file}"
            done > "$TEMP_DIR/${repo}_parsed.txt"
    fi

    rm -f "$temp_html"
    cat "$TEMP_DIR/${repo}_parsed.txt"
}

# Get all remote files for a repo in one go
get_remote_files_bulk() {
    local repo=$1
    local rel_dir="$repo/os/$ARCH"
    echo "[INFO] Fetching remote file list for $repo from $REMOTE_DIR/$rel_dir ..." >&2

    local dir_check=$(lftp -u "$LFTP_USER","$LFTP_PASS" "$LFTP_HOST" <<-EOF 2>&1
	set ftp:passive-mode yes;
	set net:timeout 30;
	cd $REMOTE_DIR/$rel_dir && echo "DIR_EXISTS" || echo "DIR_NOT_FOUND";
	bye
	EOF
    )

    if echo "$dir_check" | grep -q "DIR_NOT_FOUND"; then
        echo "[WARN] Remote directory does not exist: $REMOTE_DIR/$rel_dir" >&2
        return 0
    fi

    lftp -u "$LFTP_USER","$LFTP_PASS" "$LFTP_HOST" <<-EOF 2>&1 \
        | grep -v '^total\|^cd ok\|^DIR_EXISTS' \
        | awk '{print $NF}' \
        | grep -vE '^\.$|^\.\.$' \
        | grep -E '\.(pkg\.tar\.zst|sig|db|files)$'
	set ftp:passive-mode yes;
	set net:timeout 30;
	set net:max-retries 2;
	cd $REMOTE_DIR/$rel_dir;
	cls -1;
	bye
	EOF
}

ensure_remote_dir() {
    local rel_dir=$1
    lftp -u "$LFTP_USER","$LFTP_PASS" "$LFTP_HOST" <<-EOF >/dev/null 2>&1
	set ftp:passive-mode yes;
	set net:timeout 30;
	mkdir -p $REMOTE_DIR/$rel_dir;
	bye
	EOF
}

upload_file() {
    local input="$1"
    local url="${input%%|*}"
    local rel_path="${input##*|}"
    local remote_dir="$(dirname "$rel_path")"
    local file_name="$(basename "$rel_path")"

    local temp_file=$(mktemp "$TEMP_DIR/mirror.XXXXXXXXXX")

    for ((i=1; i<=MAX_RETRIES; i++)); do
        echo "[INFO] Attempt $i/$MAX_RETRIES: $file_name" >&2

        if curl -fL --retry 2 --connect-timeout 30 --max-time 300 \
               -o "$temp_file" "$url" 2>/dev/null; then

            if lftp -u "$LFTP_USER","$LFTP_PASS" "$LFTP_HOST" <<-EOF >/dev/null 2>&1
				set cmd:fail-exit yes;
				set net:max-retries 2;
				set net:timeout 60;
				set ftp:passive-mode yes;
				cd $REMOTE_DIR/$remote_dir || exit 1;
				put "$temp_file" -o "$file_name";
				bye
				EOF
            then
                rm -f "$temp_file"
                echo "[OK] $file_name" >&2
                return 0
            fi
        fi

        rm -f "$temp_file"
        [ $i -lt $MAX_RETRIES ] && sleep 5
    done

    echo "[ERROR] Failed: $file_name" >&2
    return 1
}

delete_obsolete_files() {
    local repo=$1
    local rel_dir="$repo/os/$ARCH"
    local source_files_list="$TEMP_DIR/${repo}_source_names.txt"
    local remote_files_list="$TEMP_DIR/${repo}_remote.txt"

    echo "[INFO] Checking for obsolete files in $repo ..." >&2

    awk -F'|' '{print $2}' "$TEMP_DIR/${repo}_source_full.txt" \
        | awk -F'/' '{print $NF}' | sort > "$source_files_list"

    local source_count=$(wc -l < "$source_files_list")
    local remote_count=$(wc -l < "$remote_files_list")

    echo "[DEBUG] Source files: $source_count, Remote files: $remote_count" >&2

    if [ $source_count -eq 0 ]; then
        echo "[ERROR] Source file list is EMPTY! Skipping deletion to prevent data loss!" >&2
        return 1
    fi

    if [ $source_count -lt 100 ] && [ $remote_count -gt 400 ]; then
        echo "[ERROR] Source file list seems incomplete ($source_count files). Skipping deletion to prevent data loss!" >&2
        return 1
    fi

    files_to_delete=$(comm -13 "$source_files_list" "$remote_files_list")

    if [ -n "$files_to_delete" ]; then
        local delete_count=$(echo "$files_to_delete" | wc -l)
        echo "[INFO] Found $delete_count obsolete files to delete" >&2

        if [ $delete_count -gt $((remote_count / 2)) ]; then
            echo "[ERROR] Attempting to delete more than 50% of files ($delete_count/$remote_count). Aborting!" >&2
            return 1
        fi

        echo "$files_to_delete" | while read -r remote_file; do
            [ -z "$remote_file" ] && continue
            echo "[DELETE] $remote_file" >&2
            lftp -u "$LFTP_USER","$LFTP_PASS" "$LFTP_HOST" <<-EOF >/dev/null 2>&1
			set ftp:passive-mode yes;
			set net:timeout 30;
			rm -f $REMOTE_DIR/$rel_dir/$remote_file;
			bye
			EOF
        done
    else
        echo "[INFO] No obsolete files found in $repo" >&2
    fi
}

find_missing_files() {
    local repo=$1
    local source_files_list="$TEMP_DIR/${repo}_source_full.txt"
    local remote_files_list="$TEMP_DIR/${repo}_remote.txt"

    echo "[INFO] Comparing file lists for $repo ..." >&2

    while IFS='|' read -r url rel_path; do
        file_name="$(basename "$rel_path")"
        if ! grep -q "^${file_name}$" "$remote_files_list" 2>/dev/null; then
            echo "$url|$rel_path"
        fi
    done < "$source_files_list"
}

export -f upload_file urldecode
export LFTP_HOST LFTP_USER LFTP_PASS REMOTE_DIR MAX_RETRIES TEMP_DIR

echo "[INFO] Starting incremental mirror with temp directory: $TEMP_DIR"

for repo in "${REPOS[@]}"; do
    echo ""
    echo "[INFO] ======================================" >&2
    echo "[INFO] Processing repo: $repo" >&2
    echo "[INFO] ======================================" >&2

    ensure_remote_dir "$repo/os/$ARCH"

    echo "[INFO] Fetching source file list for $repo ..." >&2
    if ! get_file_list "$repo" > "$TEMP_DIR/${repo}_source_full.txt"; then
        echo "[ERROR] Failed to get source file list for $repo. Skipping!" >&2
        continue
    fi

    source_file_count=$(wc -l < "$TEMP_DIR/${repo}_source_full.txt")
    echo "[INFO] Found $source_file_count files in source for $repo" >&2

    if [ "$source_file_count" -eq 0 ]; then
        echo "[ERROR] Source file list is empty for $repo! Skipping!" >&2
        continue
    fi

    get_remote_files_bulk "$repo" | sort > "$TEMP_DIR/${repo}_remote.txt"
    remote_file_count=$(wc -l < "$TEMP_DIR/${repo}_remote.txt")
    echo "[INFO] Found $remote_file_count files on remote for $repo" >&2

    if ! delete_obsolete_files "$repo"; then
        echo "[WARN] Skipped deletion phase due to safety checks" >&2
    fi

    echo "[INFO] Finding missing files for $repo ..." >&2
    missing_count=$(find_missing_files "$repo" | tee "$TEMP_DIR/${repo}_missing.txt" | wc -l)

    if [ "$missing_count" -gt 0 ]; then
        echo "[INFO] Found $missing_count files to upload for $repo" >&2
        cat "$TEMP_DIR/${repo}_missing.txt" | xargs -r -n1 -P "$PARALLEL_JOBS" bash -c 'upload_file "$@"' _
    else
        echo "[INFO] No missing files for $repo, repository is up to date" >&2
    fi
done

echo ""
echo "[INFO] ======================================" >&2
echo "[INFO] Incremental mirroring completed!" >&2
echo "[INFO] ======================================" >&2
