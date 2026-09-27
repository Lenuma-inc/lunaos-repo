#!/bin/bash
set -euo pipefail
: "${FTP_SERVER:?set FTP_SERVER}"
: "${FTP_USERNAME:?set FTP_USERNAME}"
: "${FTP_PASSWORD:?set FTP_PASSWORD}"
: "${LOCAL_DIR:?set LOCAL_DIR}"
: "${SERVER_DIR:?set SERVER_DIR}"
# Paths are interpolated into lftp's command language, credentials are not.
for path in "$LOCAL_DIR" "$SERVER_DIR"; do
  [[ "$path" =~ ^[a-zA-Z0-9_./-]+$ ]] || { echo 'Invalid mirror path' >&2; exit 1; }
done
export LFTP_PASSWORD="$FTP_PASSWORD"
lftp --norc --env-password --user "$FTP_USERNAME" "$FTP_SERVER" <<EOF
set cmd:fail-exit yes
set net:timeout 60
set net:max-retries 3
set ftp:ssl-force yes
set ssl:check-hostname no
set mirror:set-permissions false
# Upload packages first, databases second, and only then remove obsolete files.
mirror --reverse --no-recursion --upload-older --parallel=4 --dereference --verbose --include-glob '*.pkg.tar.zst*' "$LOCAL_DIR" "${SERVER_DIR%/}"
mirror --reverse --no-recursion --upload-older --parallel=1 --dereference --verbose --exclude-glob '*.pkg.tar.zst*' "$LOCAL_DIR" "${SERVER_DIR%/}"
mirror --reverse --no-recursion --upload-older --delete --parallel=4 --dereference --verbose "$LOCAL_DIR" "${SERVER_DIR%/}"
bye
EOF
