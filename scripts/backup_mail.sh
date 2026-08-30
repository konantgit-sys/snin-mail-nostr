#!/bin/bash
# SNIN Mail backup: inbox.db + sessions + config
# Retention: 14 snapshots. Run: every 6h via cron.
set -euo pipefail

SRC="${MAIL_SRC:-${HOME:-/home/agent}/data/sites/cryter-mail}"
DEST="${MAIL_DEST:-${HOME:-/home/agent}/data/backups/mail}"
KEEP=14
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$DEST"

# sqlite: безопасная онлайн-копия через backup API
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$SRC/inbox.db" ".backup '$DEST/inbox-$STAMP.db'" 2>/dev/null \
        || cp "$SRC/inbox.db" "$DEST/inbox-$STAMP.db"
else
    cp "$SRC/inbox.db" "$DEST/inbox-$STAMP.db"
fi
[ -f "$SRC/.sessions.json" ] && cp "$SRC/.sessions.json" "$DEST/sessions-$STAMP.json"
[ -f "$SRC/config.json" ] && cp "$SRC/config.json" "$DEST/config-$STAMP.json"

# Rotate
ls -1t "$DEST"/inbox-*.db 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f
ls -1t "$DEST"/sessions-*.json 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f
ls -1t "$DEST"/config-*.json 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f

echo "$(date -Iseconds) mail backup ok: $DEST (inbox-${STAMP}.db)"
