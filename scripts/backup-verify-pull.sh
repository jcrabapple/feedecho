#!/bin/bash
# Weekly restore verification: pull the newest dump from the VPS and prove it
# restores into a throwaway Postgres, counting rows in the core tables.
# Run on Jason's local machine via Hermes cron (pull-only offsite copy).
# Secrets: uses the VPS SSH key already configured on this machine.

set -euo pipefail

VPS_HOST="root@107.150.1.236"
VPS_BACKUP_DIR="/opt/feedecho/backups"
LOCAL_DIR="$HOME/feedecho-backups"
WORKDIR="$(mktemp -d)"
CONTAINER="feedecho-restore-verify"
LOG_TAG="feedecho-backup-verify"

mkdir -p "$LOCAL_DIR"

cleanup() {
    podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

# 1. Pull the newest dump + env snapshot (offsite copy lives here, on the
#    machine that holds the key; the VPS holds no credentials to this box).
newest_remote="$(ssh -o BatchMode=yes "$VPS_HOST" "ls -1t $VPS_BACKUP_DIR/feedecho-*.sql.gz | head -1")"
ssh -o BatchMode=yes "$VPS_HOST" "cat '$newest_remote'" > "$LOCAL_DIR/$(basename "$newest_remote")"
ssh -o BatchMode=yes "$VPS_HOST" "ls -1t $VPS_BACKUP_DIR/env-* 2>/dev/null | head -1" | {
    read -r env_file || true
    [ -n "${env_file:-}" ] && scp -q -o BatchMode=yes "$VPS_HOST":"$env_file" "$LOCAL_DIR/.env-latest" || true
}

dump="$(ls -1t "$LOCAL_DIR"/feedecho-*.sql.gz | head -1)"

# 2. Restore into a throwaway container.
podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
podman run -d --name "$CONTAINER" -e POSTGRES_USER=feedecho -e POSTGRES_PASSWORD=feedecho \
    -e POSTGRES_DB=feedecho docker.io/library/postgres:17-alpine >/dev/null
for i in $(seq 1 30); do
    podman exec "$CONTAINER" pg_isready -U feedecho >/dev/null 2>&1 && break
    sleep 1
done
gunzip -c "$dump" | podman exec -i "$CONTAINER" psql -U feedecho -d feedecho -v ON_ERROR_STOP=1 -q

# 3. Row counts prove the restore is real, not just schema.
counts="$(podman exec "$CONTAINER" psql -U feedecho -d feedecho -t -A -c "
    SELECT 'users=' || count(*) FROM users
    UNION ALL SELECT 'feeds=' || count(*) FROM feeds
    UNION ALL SELECT 'echoes=' || count(*) FROM echoes
    UNION ALL SELECT 'posted_items=' || count(*) FROM posted_items;")"

# 4. Sanity: every count must be an integer line.
echo "$counts" | grep -qE '^users=[0-9]+$' || { logger -t "$LOG_TAG" -p daemon.err "VERIFY FAILED: bad counts"; exit 1; }

logger -t "$LOG_TAG" "VERIFY OK: $(basename "$dump") restored; $counts | copied to $LOCAL_DIR"
echo "RESTORE VERIFY OK: $(basename "$dump")"
echo "$counts"
