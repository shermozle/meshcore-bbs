#!/usr/bin/env bash
# Quick BBS DB inspection.
# Usage: inspect_db.sh <db_path>

set -euo pipefail

DB="${1:-/data/bbs.db}"
if [[ ! -f "$DB" ]]; then
    echo "DB not found: $DB" >&2
    exit 1
fi

echo "=== BBS DB summary: $DB ==="
echo

sqlite3 -header -column "$DB" <<'SQL'
SELECT 'users' AS table_name, COUNT(*) AS rows FROM users
UNION ALL SELECT 'onboarded users', COUNT(*) FROM users WHERE onboarded=1
UNION ALL SELECT 'banned users', COUNT(*) FROM users WHERE banned=1
UNION ALL SELECT 'boards', COUNT(*) FROM boards
UNION ALL SELECT 'board_posts', COUNT(*) FROM board_posts WHERE deleted=0
UNION ALL SELECT 'mail (unread)', COUNT(*) FROM mail WHERE read_at IS NULL AND deleted=0
UNION ALL SELECT 'mail (read)', COUNT(*) FROM mail WHERE read_at IS NOT NULL AND deleted=0
UNION ALL SELECT 'news_items', COUNT(*) FROM news_items
UNION ALL SELECT 'audit_log', COUNT(*) FROM audit_log;
SQL

echo
echo "=== Outbound queue ==="
sqlite3 -header -column "$DB" "SELECT status, COUNT(*) AS n FROM outbound_queue GROUP BY status"

echo
echo "=== Recent activity (last 10 users) ==="
sqlite3 -header -column "$DB" <<'SQL'
SELECT
    COALESCE(display_name, substr(pubkey,1,8)) AS who,
    msg_count,
    datetime(last_seen, 'unixepoch') AS last_seen,
    CASE banned WHEN 1 THEN 'BANNED' ELSE '' END AS flag
FROM users
ORDER BY last_seen DESC
LIMIT 10;
SQL

echo
echo "=== Recent admin actions ==="
sqlite3 -header -column "$DB" <<'SQL'
SELECT
    datetime(ts, 'unixepoch') AS when_,
    substr(COALESCE(actor_pubkey, '<system>'), 1, 12) AS actor,
    action,
    substr(detail, 1, 60) AS detail
FROM audit_log
ORDER BY ts DESC
LIMIT 20;
SQL
