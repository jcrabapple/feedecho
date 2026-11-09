#!/usr/bin/env python3
"""Baseline-ratchet audit gate, ported from ruflo's ADR-112 tool-descriptions audit.

Scans a codebase for a countable violation class, compares the count against a
monotone-decreasing baseline in JSON, and fails CI if the count rises. Supports
`--update-baseline` to lock in a new floor after a deliberate change. This is the
gate that stops "just one more violation" creeping in on a branch nobody is watching.

Usage:
  baseline_audit.py scan <root> --pattern '<regex>' [--glob '*.py'] \\
      [--exclude '<regex>'] [--baseline <path>] [--max N] [--json] [--update-baseline]

First run without a baseline: reports count, writes baseline on --update-baseline.
Later runs: fail (exit 1) if count > baseline.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys


def iter_files(root: str, glob: str, exclude: re.Pattern | None) -> list[str]:
    import fnmatch
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__", ".hermes-cache", "dist", "build", ".venv", "venv")]
        for fn in filenames:
            if not fnmatch.fnmatch(fn, glob):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root)
            if exclude and exclude.search(rel):
                continue
            out.append(p)
    return sorted(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="scan and compare against baseline")
    p.add_argument("root")
    p.add_argument("--pattern", required=True, help="regex; each match counts as one violation")
    p.add_argument("--glob", default="*.py")
    p.add_argument("--exclude", default=None, help="regex on relative path to skip")
    p.add_argument("--baseline", default=None, help="baseline JSON path (default <root>/verification/baseline.json)")
    p.add_argument("--max", type=int, default=None, help="hard ceiling; fail if count > N (no baseline file needed)")
    p.add_argument("--name", default="default", help="baseline key if sharing one file across checks")
    p.add_argument("--update-baseline", action="store_true", dest="update_baseline")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--multiline", action="store_true",
                   help="match against whole files (pattern may span lines; MULTILINE flag set)")
    args = ap.parse_args(argv)

    baseline_path = args.baseline or os.path.join(args.root, "verification", "baseline.json")
    pattern = re.compile(args.pattern, re.MULTILINE if args.multiline else 0)
    exclude = re.compile(args.exclude) if args.exclude else None

    matches = []
    for path in iter_files(args.root, args.glob, exclude):
        rel = os.path.relpath(path, args.root)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if args.multiline:
            # Whole-file search so a pattern can span lines
            # (e.g. 'except Exception:\s*\n\s*pass\b').
            for m in pattern.finditer(text):
                matches.append({"file": rel, "line": text.count("\n", 0, m.start()) + 1})
        else:
            for i, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    matches.append({"file": rel, "line": i})

    count = len(matches)
    per_file: dict[str, int] = {}
    for m in matches:
        per_file[m["file"]] = per_file.get(m["file"], 0) + 1

    baseline = {}
    if os.path.isfile(baseline_path) and os.path.getsize(baseline_path):
        with open(baseline_path, encoding="utf-8") as f:
            baseline = json.load(f)
    entry = baseline.get(args.name)
    # Entry is {"count": N, "files": {path: n}}; a bare int (older format)
    # still gates the total, it just cannot attribute the increase.
    if isinstance(entry, dict):
        prev, prev_files = entry.get("count"), entry.get("files")
    else:
        prev, prev_files = entry, None

    if prev is not None and count > prev and not args.update_baseline:
        # Blame files whose own count rose; list all their matches (we cannot
        # tell which line in a file is the new one, only that the file grew).
        if prev_files is not None:
            grew = {f for f, n in per_file.items() if n > prev_files.get(f, 0)}
            suspects = [m for m in matches if m["file"] in grew]
        else:
            suspects = matches
        result = {
            "ok": False,
            "name": args.name,
            "count": count,
            "baseline": prev,
            "delta": count - prev,
            "new_matches": suspects,
        }
        if args.as_json:
            print(json.dumps(result, indent=2))
        else:
            print(f"BASELINE REGRESSION [{args.name}]: {count} > {prev} (+{count - prev})")
            if prev_files is None:
                print("  (baseline has no per-file counts; showing all matches — re-lock with --update-baseline)")
            else:
                print("  in files whose count rose:")
            for m in result["new_matches"][:20]:
                print(f"  {m['file']}:{m['line']}")
            if len(result["new_matches"]) > 20:
                print(f"  … and {len(result['new_matches']) - 20} more")
            print(f"  fix the violations or, if deliberate, re-run with --update-baseline")
        return 1

    if args.update_baseline:
        os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
        baseline[args.name] = {"count": count, "files": dict(sorted(per_file.items()))}
        with open(baseline_path, "w", encoding="utf-8") as f:
            json.dump(baseline, f, indent=2, sort_keys=True)
            f.write("\n")
        verb = "locked new baseline" if prev is not None else "created baseline"

    result = {
        "ok": True,
        "name": args.name,
        "count": count,
        "baseline": prev if prev is not None else count,
        "delta": (count - prev) if prev is not None else 0,
    }
    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        state = "OK" if prev is None or count <= prev else "OK (baseline updated)"
        detail = f" (baseline {prev})" if prev is not None else " (no prior baseline)"
        if args.update_baseline:
            detail += f" — {verb}"
        elif prev is not None and count < prev:
            detail += " — count dropped; run with --update-baseline to lock the lower floor"
        print(f"{state} [{args.name}]: {count} matches{detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
