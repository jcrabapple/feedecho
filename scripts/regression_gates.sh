#!/usr/bin/env bash
# Regression gates: documented-fix witness + monotone-decreasing audit ratchets.
#
#   scripts/regression_gates.sh                    # check (CI + local)
#   scripts/regression_gates.sh --update-baseline  # lock a deliberate new count
#
# Witness: every entry in witness/witness-fixes.json names a file and a marker
# substring the fix created (optionally a `forbid` regex that must stay absent).
# A refactor that deletes the load-bearing line fails here even when the suite
# is green. After adding a fix entry, re-sign the manifest:
#
#   python scripts/witness.py regen --root . --dir witness
#
# and commit witness/ together. `history regressions` names the commit window a
# fix disappeared in. Tooling ported from ruvnet/ruflo's verification stack (MIT).
set -uo pipefail   # no -e: run every gate, report all failures, exit nonzero once
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"

UPDATE=()
[ "${1:-}" = "--update-baseline" ] && UPDATE=(--update-baseline)
rc=0

"$PY" scripts/witness.py verify \
    --manifest witness/linux/manifest.md.json \
    --fixes witness/witness-fixes.json \
    --root . || rc=1

# Counts may only go down. Raising one needs --update-baseline in the same
# commit as the new occurrence, so the diff shows it to review.
EXCLUDE='^(tests|scripts|dist|docs|nix|static)/'
audit() {  # name pattern [extra flags...]
    local name="$1" pattern="$2"; shift 2
    "$PY" scripts/baseline_audit.py scan . --pattern "$pattern" \
        --exclude "$EXCLUDE" --baseline witness/baseline.json \
        --name "$name" "$@" "${UPDATE[@]}" || rc=1
}
audit noqa                '#\s*noqa'
audit type-ignore         '#\s*type:\s*ignore'
audit bare-except         '^\s*except\s*:'
audit swallowed-exception 'except\s+Exception\s*:\s*\n\s*pass\b' --multiline

exit "$rc"
