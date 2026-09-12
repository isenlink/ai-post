#!/usr/bin/env bash
# ai-post log archival: export messages older than 30 days to JSONL,
# delete them from the main DB, back up config, prune old archives.
# Point AI_POST_DIR / AI_POST_BACKUP at your paths (or edit below).
set -euo pipefail
DIR="${AI_POST_DIR:-/opt/ai-post}"
BACKUP="${AI_POST_BACKUP:-$DIR/archive}"
RETENTION_DAYS="${AI_POST_RETENTION_DAYS:-30}"
ARCHIVE_KEEP_DAYS="${AI_POST_ARCHIVE_KEEP_DAYS:-180}"
mkdir -p "$BACKUP"
CUTOFF=$(python3 -c "import time; print(time.time() - $RETENTION_DAYS*86400)")
STAMP=$(date +%Y%m%d)
python3 - "$DIR/ai_post.db" "$BACKUP" "$CUTOFF" "$STAMP" <<'EOF'
import json, os, sqlite3, sys
db, backup, cutoff, stamp = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM messages WHERE ts < ? ORDER BY id", (cutoff,)).fetchall()
if rows:
    path = os.path.join(backup, f"messages-{stamp}.jsonl")
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
    conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
    conn.commit()
    print(f"archived {len(rows)} messages -> {path}")
else:
    print("nothing to archive")
EOF
# back up config (contains tokens, keep 600)
if [ -f "$DIR/config.json" ]; then
    cp -p "$DIR/config.json" "$BACKUP/config.json.$STAMP"
    chmod 600 "$BACKUP"/config.json.* 2>/dev/null || true
fi
# prune old archives
find "$BACKUP" -name 'messages-*.jsonl' -mtime +"$ARCHIVE_KEEP_DAYS" -delete
