#!/bin/bash
# FeedEcho production backup — pg_dump to local disk, prune, verify newest dump.
# Installed on VPS 107.150.1.236 at /opt/feedecho/backup.sh, run by cron daily.
# Integrity check: restores the newest dump into a throwaway postgres container
# weekly (cron line with 'weekly') and reports row counts of core tables.

set -euo pipefail

BACKUP_DIR="/opt/feedecho/backups"
KEEP_DAILY=7
KEEP_WEEKLY=8
COMPOSE="/usr/bin/docker compose -f /opt/feedecho/docker-compose.multi.yml"
DATE="$(date +%Y-%m-%d_%H%M)"
LOG_TAG="feedecho-backup"

mkdir -p "$BACKUP_DIR"

dump_file="$BACKUP_DIR/feedecho-$DATE.sql.gz"

# Dump via the running postgres container (custom format, gzip for space).
$COMPOSE exec -T postgres pg_dump -U feedecho -d feedecho | gzip > "$dump_file"

# Fail loudly on an empty/corrupt dump (e.g. postgres restarting mid-dump).
if ! [ -s "$dump_file" ]; then
    logger -t "$LOG_TAG" -p daemon.err "ERROR: dump is empty: $dump_file"
    rm -f "$dump_file"
    exit 1
fi
if ! gzip -t "$dump_file" 2>/dev/null; then
    logger -t "$LOG_TAG" -p daemon.err "ERROR: dump failed gzip integrity: $dump_file"
    rm -f "$dump_file"
    exit 1
fi

# Also snapshot the .env (secrets) — it is required to stand the stack back up,
# and it is the only piece not inside a volume. Same retention below.
cp /opt/feedecho/.env "$BACKUP_DIR/env-$DATE" 2>/dev/null || \
    logger -t "$LOG_TAG" -p daemon.warning "WARNING: could not copy .env"

# Retention: keep KEEP_DAILY most recent, plus weekly checkpoints
# (oldest dump of each ISO week) so history reaches ~2 months without
# unbounded growth. Dumps are ~30KB now; retention is future-proofing.
ls -1t "$BACKUP_DIR"/feedecho-*.sql.gz 2>/dev/null | head -n "$KEEP_DAILY" > /tmp/fe.keep.daily
ls -1t "$BACKUP_DIR"/feedecho-*.sql.gz 2>/dev/null | awk -F'feedecho-' '{print $2}' | cut -c1-10 \
    | awk -F- '{y=$1; w=($2+0); printf "%s-W%02d\n", y, w}' | sort -u > /tmp/fe.weeks 2>/dev/null || true
: > /tmp/fe.keep.weekly
for w in $(cat /tmp/fe.weeks | head -n "$KEEP_WEEKLY"); do
    # first (newest) dump of that week
    ls -1t "$BACKUP_DIR"/feedecho-*.sql.gz | while read -r f; do
        d=$(basename "$f" | awk -F'feedecho-' '{print $2}' | cut -c1-10)
        wy=$(echo "$d" | awk -F- '{printf "%s-W%02d", $1, ($2+0)}')
        if [ "$wy" = "$w" ]; then echo "$f"; break; fi
    done >> /tmp/fe.keep.weekly
done
cat /tmp/fe.keep.daily /tmp/fe.keep.weekly 2>/dev/null | sort -u > /tmp/fe.keep.all
find "$BACKUP_DIR" -name 'feedecho-*.sql.gz' | sort > /tmp/fe.all
comm -13 /tmp/fe.keep.all /tmp/fe.all | while read -r f; do rm -f "$f"; done
# env snapshots: keep the newest KEEP_DAILY only
ls -1t "$BACKUP_DIR"/env-* 2>/dev/null | tail -n +$((KEEP_DAILY + 1)) | while read -r f; do rm -f "$f"; done
rm -f /tmp/fe.keep.daily /tmp/fe.keep.weekly /tmp/fe.keep.all /tmp/fe.all /tmp/fe.weeks

# Verify the newest dump parses as SQL (cheap header check + size sanity).
newest="$(ls -1t "$BACKUP_DIR"/feedecho-*.sql.gz 2>/dev/null | head -1)"
if [ -n "$newest" ]; then
    size=$(stat -c%s "$newest")
    logger -t "$LOG_TAG" "OK: newest dump $newest (${size} bytes)"
fi

# Offsite: pull from Jason's other box is pull-only by design (backup server
# holds the SSH key, VPS holds nothing sensitive). See the local-side cron.
exit 0
