#!/usr/bin/env bash
# Backup the BBS SQLite database using the online backup API.
# Usage: backup_db.sh <data_dir> <backup_dir>
#
# Uses sqlite3's `.backup` rather than a plain file copy, which is safe even
# while the BBS is writing.

set -euo pipefail

DATA_DIR="${1:-/data}"
BACKUP_DIR="${2:-/data/backups}"

DB_PATH="${DATA_DIR}/bbs.db"
DATE=$(date -u +%Y%m%d-%H%M%S)
OUT="${BACKUP_DIR}/bbs-${DATE}.db"

if [[ ! -f "$DB_PATH" ]]; then
    echo "DB not found: $DB_PATH" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

echo "Backing up $DB_PATH -> $OUT"
sqlite3 "$DB_PATH" ".backup '$OUT'"

# Quick integrity check of the copy
echo "Verifying backup..."
result=$(sqlite3 "$OUT" "PRAGMA integrity_check")
if [[ "$result" != "ok" ]]; then
    echo "integrity check FAILED: $result" >&2
    exit 2
fi

# Compress to save space
gzip -f "$OUT"
echo "Backup OK: ${OUT}.gz ($(du -h "${OUT}.gz" | cut -f1))"

# Retention: keep 30 most recent
echo "Pruning old backups..."
ls -1t "${BACKUP_DIR}"/bbs-*.db.gz 2>/dev/null | tail -n +31 | xargs -r rm -v
