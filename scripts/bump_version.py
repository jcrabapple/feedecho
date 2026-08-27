#!/usr/bin/env python3
"""Bump the version everywhere it is hardcoded.

The Python side reads ``_version.py`` (``app.py`` for FastAPI/OpenAPI and the
footer, hatchling for packaging metadata). The deploy artifacts cannot import
Python at evaluation time, so they each keep a literal copy — this script
rewrites all of them together and asserts an expected count per file, so a new
occurrence added later fails loudly instead of silently shipping a stale value.

Usage:
    python scripts/bump_version.py X.Y.Z

``tests/test_version.py`` re-checks the same invariant in CI.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# file -> how many times the bare version string must appear in it
TARGETS: dict[str, int] = {
    "_version.py": 1,
    "flake.nix": 1,
    # version, rev, and the nix-prefetch-url example URL in the hash comment
    "nix/package.nix": 3,
    # rev + tarball URL
    "nix/README.md": 2,
}
# The hosted deployment's compose file (with its pinned GHCR image tag) moved
# to the private feedecho-hosted overlay (2026-08-27, product-split directive).
# After bumping, copy the new tag into
# ~/feedecho-hosted/configs/docker-compose.multi.yml before deploying.

# Files that deliberately name the version a past change shipped in. These are
# history, not configuration: never rewrite them.
HISTORICAL = {
    "Dockerfile",
    "docker-entrypoint.sh",
    "tests/test_review_fixes.py",
}


def current_version() -> str:
    text = (ROOT / "_version.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if not m:
        sys.exit("could not find __version__ in _version.py")
    return m.group(1)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return int(bool(sys.stderr.write(f"usage: {argv[0]} <new-version>\n")))
    new = argv[1].lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", new):
        sys.exit(f"not a semver version: {new!r}")

    old = current_version()
    if old == new:
        sys.exit(f"already at {new}")

    # Pass 1: verify every expected count before writing anything, so a
    # mismatch cannot leave the tree half-rewritten.
    counts = {}
    for rel, expected in TARGETS.items():
        path = ROOT / rel
        if not path.exists():
            sys.exit(f"missing {rel}")
        found = path.read_text(encoding="utf-8").count(old)
        counts[rel] = found
        if found != expected:
            sys.exit(
                f"{rel}: expected {expected} occurrence(s) of {old}, found {found}."
                " Update TARGETS in this script (and tests/test_version.py) first."
            )

    for rel in TARGETS:
        path = ROOT / rel
        path.write_text(
            path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8"
        )
        print(f"  {rel}: {counts[rel]} occurrence(s) {old} -> {new}")

    # Anything left over that is not a deliberate historical mention.
    leftovers = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if (
            rel in HISTORICAL
            or rel.startswith((".git/", ".venv/", "dist/", "docs/"))
            or "__pycache__" in rel
            or path.suffix in {".db", ".lock", ".png", ".whl"}
        ):
            continue
        try:
            if old in path.read_text(encoding="utf-8"):
                leftovers.append(rel)
        except (UnicodeDecodeError, OSError):
            continue
    if leftovers:
        print(f"\nWARNING: {old} still appears in: {', '.join(sorted(leftovers))}")
        print("Check whether those are historical references or a missed target.")

    print(f"\nBumped {old} -> {new}. Next: run the suites, then tag v{new}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
