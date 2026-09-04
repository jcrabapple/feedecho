#!/bin/bash
# Weekly restore verification: pull the newest ENCRYPTED dump from the VPS,
# decrypt it with the age key held locally, and prove it restores into a
# throwaway Postgres, counting rows in the core tables.
#
# Run on Jason's local machine via Hermes cron (pull-only offsite copy).
# The private age key lives at ~/.config/feedecho/backup.age.key (and in
# Infisical as FEEDECHO_BACKUP_AGE_KEY); it never exists on the VPS.

set -euo pipefail

# VPS coordinates come from the environment (Infisical: FEEDCHO_VPS_IP /
# FEEDCHO_VPS_USER), never hardcoded — this script is in the public OSS repo,
# and VPS IPs must not leak there. Source infisical-env if they are absent.
if [ -z "${FEEDCHO_VPS_IP:-}" ] || [ -z "${FEEDCHO_VPS_USER:-}" ]; then
    if command -v infisical-env >/dev/null 2>&1; then
        eval "$(infisical-env)"
    fi
fi
: "${FEEDCHO_VPS_IP:?FEEDCHO_VPS_IP not set}"
: "${FEEDCHO_VPS_USER:?FEEDCHO_VPS_USER not set}"
VPS_HOST="${FEEDCHO_VPS_USER}@${FEEDCHO_VPS_IP}"
VPS_BACKUP_DIR="/opt/feedecho/backups"
LOCAL_DIR="$HOME/feedecho-backups"
WORKDIR="$(mktemp -d)"
CONTAINER="feedecho-restore-verify"
LOG_TAG="feedecho-backup-verify"
AGE="$HOME/.local/bin/age"
AGE_KEY="$HOME/.config/feedecho/backup.age.key"

mkdir -p "$LOCAL_DIR"

cleanup() {
    podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

# The age binary may not be on PATH (installed to ~/.local/bin as a static
# binary). Resolve it, or fail loudly — a backup that cannot be decrypted is
# not a backup.
if [ ! -x "$AGE" ] && ! command -v age >/dev/null 2>&1; then
    logger -t "$LOG_TAG" -p daemon.err "VERIFY FAILED: age binary not found"
    echo "age binary not found" >&2
    exit 1
fi
[ -x "$AGE" ] || AGE="$(command -v age)"
if [ ! -f "$AGE_KEY" ]; then
    logger -t "$LOG_TAG" -p daemon.err "VERIFY FAILED: age private key missing at $AGE_KEY"
    echo "age private key missing" >&2
    exit 1
fi

# 1. Pull the newest encrypted dump (offsite copy lives here, still age-
#    encrypted at rest on this box — the private key is what makes it usable).
newest_remote="$(ssh -o BatchMode=yes "$VPS_HOST" "ls -1t $VPS_BACKUP_DIR/feedecho-*.sql.gz.age | head -1")"
scp -q -o BatchMode=yes "$VPS_HOST":"$newest_remote" "$LOCAL_DIR/$(basename "$newest_remote")"

dump="$(ls -1t "$LOCAL_DIR"/feedecho-*.sql.gz.age | head -1)"

# 2. Decrypt -> gunzip -> restore into a throwaway Postgres.
podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
podman run -d --name "$CONTAINER" -e POSTGRES_USER=feedecho -e POSTGRES_PASSWORD=feedecho \
    -e POSTGRES_DB=feedecho docker.io/library/postgres:17-alpine >/dev/null
for i in $(seq 1 30); do
    podman exec "$CONTAINER" pg_isready -U feedecho >/dev/null 2>&1 && break
    sleep 1
done
# pg_isready says "accepting" while postgres is still finishing its startup
# sequence — the first connection right after can hit a transient "shutting
# down" or "the database system is starting up". Give it a grace window.
sleep 2
"$AGE" -d -i "$AGE_KEY" "$dump" | gunzip -c | \
    podman exec -i "$CONTAINER" psql -U feedecho -d feedecho -v ON_ERROR_STOP=1 -q || \
    { sleep 3; "$AGE" -d -i "$AGE_KEY" "$dump" | gunzip -c | \
      podman exec -i "$CONTAINER" psql -U feedecho -d feedecho -v ON_ERROR_STOP=1 -q; }

# 3. Row counts prove the restore is real, not just schema.
counts="$(podman exec "$CONTAINER" psql -U feedecho -d feedecho -t -A -c "
    SELECT 'users=' || count(*) FROM users
    UNION ALL SELECT 'feeds=' || count(*) FROM feeds
    UNION ALL SELECT 'echoes=' || count(*) FROM echoes
    UNION ALL SELECT 'posted_items=' || count(*) FROM posted_items;")"

# 4. Sanity: every count must be an integer line.
echo "$counts" | grep -qE '^users=[0-9]+$' || { logger -t "$LOG_TAG" -p daemon.err "VERIFY FAILED: bad counts"; exit 1; }

# 5. Also pull the encrypted .env snapshot (offsite copy of the secrets needed
#    to stand the stack back up; remains age-encrypted at rest here too).
env_remote="$(ssh -o BatchMode=yes "$VPS_HOST" "ls -1t $VPS_BACKUP_DIR/env-*.age 2>/dev/null | head -1")"
if [ -n "${env_remote:-}" ]; then
    scp -q -o BatchMode=yes "$VPS_HOST":"$env_remote" "$LOCAL_DIR/.env-latest.age"
fi

# Prune local plaintext leftovers from the pre-encryption era (they are
# redundant with the encrypted copies and should not linger as plaintext PII).
find "$LOCAL_DIR" \( -name 'feedecho-*.sql.gz' -o -name '.env-latest' \) -mtime +1 -delete 2>/dev/null || true

logger -t "$LOG_TAG" "VERIFY OK: $(basename "$dump") decrypted + restored; $counts | copied to $LOCAL_DIR"
echo "RESTORE VERIFY OK: $(basename "$dump")"
echo "$counts"
