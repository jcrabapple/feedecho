#!/usr/bin/env python3
"""Witness manifest toolkit: cryptographic regression protection for documented fixes.

Port of ruflo's witness system (github.com/ruvnet/ruflo, MIT) to a Python CLI
(stdlib + cryptography or pynacl for the signature). Every documented fix gets a SHA-256 file fingerprint + a marker substring
that must remain present, recorded in an Ed25519-signed per-OS manifest, with an
append-only JSONL history so a regression can be bisected to the commit window that
introduced it.

The Ed25519 key is deterministically derived from the git commit
(sha256(commit + ':witness/v1')), so anyone holding the commit can re-derive the
public key and verify the signature with no committed secret. This proves the
manifest matches the tree and detects tampering/drift; it does not defend against
the committer (who can always re-sign). It is provenance, not a security boundary.

Commands:
  init     --root .                      # template + empty per-OS manifest/history
  regen    [--root .] [--commit SHA]     # re-scan fixes, re-sign, append history
  verify   --manifest verification/<os>/manifest.md.json [--json]
  history  --history verification/<os>/history.jsonl summary|regressions|timeline [--id FIX]

Exit codes: 0 = pass (drift is a warning), 1 = regression/missing/tamper/invalid.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time

WITNESS_DOMAIN = ":witness/v1"
SCHEMA = "witness/v1"

def _load_backend():
    """Return (sign, verify) using cryptography or pynacl; None if neither is installed.

    No hand-rolled Ed25519: the seed is derived from a public commit hash, so the
    signature adds tamper-evidence only. Not worth a homemade curve implementation.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey, Ed25519PublicKey,
        )

        def sign(seed, msg):
            k = Ed25519PrivateKey.from_private_bytes(seed)
            return k.sign(msg), k.public_key().public_bytes_raw()

        def verify(pub, sig, msg):
            try:
                Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
                return True
            except Exception:
                return False
        return sign, verify
    except ImportError:
        pass
    try:
        from nacl.signing import SigningKey, VerifyKey

        def sign(seed, msg):
            k = SigningKey(seed)
            return k.sign(msg).signature, k.verify_key.encode()

        def verify(pub, sig, msg):
            try:
                VerifyKey(pub).verify(msg, sig)
                return True
            except Exception:
                return False
        return sign, verify
    except ImportError:
        return None


_BACKEND = _load_backend()


def _sign(seed: bytes, msg: bytes):
    if _BACKEND is None:
        return b"", b""
    return _BACKEND[0](seed, msg)


def _verify(pub: bytes, sig: bytes, msg: bytes):
    """True/False, or None when no Ed25519 backend is available."""
    if _BACKEND is None or not sig:
        return None
    return _BACKEND[1](pub, sig, msg)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_file(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text_normalized(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        data = f.read()
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def os_slug() -> str:
    s = platform.system().lower()
    return {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(s, s or "unknown")


def git_commit(root: str, override: str | None) -> str:
    if override:
        return override
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    print("WARNING: not a git repo; using placeholder commit", file=sys.stderr)
    return "uncommitted"


def default_paths(root: str, wdir: str = "verification") -> tuple[str, str, str]:
    os_dir = os.path.join(root, wdir, os_slug())
    return (
        os.path.join(root, wdir, "witness-fixes.json"),
        os.path.join(os_dir, "manifest.md.json"),
        os.path.join(os_dir, "history.jsonl"),
    )


def load_fixes(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    fixes = data.get("fixes", [])
    for fx in fixes:
        for key in ("id", "desc", "file", "marker"):
            if key not in fx:
                raise SystemExit(f"{path}: fix missing required field '{key}': {fx}")
    return fixes


def fix_holds(fx: dict, text: str | None) -> bool:
    """Marker present and (when set) the `forbid` regex absent.

    `forbid` covers fixes that are an ABSENCE: a removed directive, a banned
    call, a header value that must never come back. Multiline regex, so
    `^USER ` anchors to a line start.
    """
    if text is None or fx["marker"] not in text:
        return False
    forbid = fx.get("forbid")
    return not (forbid and re.search(forbid, text, re.MULTILINE))


def scan_fixes(root: str, fixes: list[dict]) -> list[dict]:
    out = []
    for fx in fixes:
        path = os.path.join(root, fx["file"])
        digest = sha256_file(path)
        text = read_text_normalized(path)
        if digest is None:
            status = "missing"
            marker_ok = False
        else:
            marker_ok = fix_holds(fx, text)
            if marker_ok and fx.get("_prev_sha256") is None:
                status = "verified"  # first snapshot establishes the fingerprint
            elif marker_ok and digest == fx["_prev_sha256"]:
                status = "verified"
            elif marker_ok:
                status = "drift"
            else:
                status = "regressed"
        entry = {
            "id": fx["id"],
            "desc": fx["desc"],
            "file": fx["file"],
            "sha256": digest or "",
            "marker": fx["marker"],
            "markerVerified": marker_ok,
            "status": status,
        }
        if fx.get("forbid"):
            entry["forbid"] = fx["forbid"]
        out.append(entry)
    return out


def derive_seed(commit: str) -> bytes:
    return hashlib.sha256((commit + WITNESS_DOMAIN).encode()).digest()


def build_manifest(root: str, commit: str, fixes: list[dict]) -> dict:
    return {
        "schema": SCHEMA,
        "issuedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gitCommit": commit,
        "branch": git_branch(root),
        "os": os_slug(),
        "summary": {
            "totalFixes": len(fixes),
            "verified": sum(1 for f in fixes if f["status"] == "verified"),
            "drift": sum(1 for f in fixes if f["status"] == "drift"),
            "regressed": sum(1 for f in fixes if f["status"] == "regressed"),
            "missing": sum(1 for f in fixes if f["status"] == "missing"),
        },
        "fixes": fixes,
    }


def git_branch(root: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def write_manifest(path: str, manifest: dict) -> dict:
    canon = canonical(manifest)
    digest = hashlib.sha256(canon).hexdigest()
    sig, pub = _sign(derive_seed(manifest["gitCommit"]), canon)
    integrity = {
        "manifestHashAlgo": "sha256",
        "manifestHash": digest,
        "signatureAlgo": "ed25519",
        "publicKey": pub.hex(),
        "signature": sig.hex(),
        "seedDerivation": "sha256(gitCommit + ':witness/v1')",
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"manifest": manifest, "integrity": integrity}, f, indent=2, sort_keys=True)
        f.write("\n")
    return integrity


def append_history(path: str, manifest: dict) -> None:
    entry = {
        "v": 1,
        "commit": manifest["gitCommit"],
        "issuedAt": manifest["issuedAt"],
        "branch": manifest["branch"],
        "os": manifest["os"],
        "manifestHash": hashlib.sha256(canonical(manifest)).hexdigest(),
        "summary": manifest["summary"],
        "fixes": {
            f["id"]: {"sha256": f["sha256"], "markerVerified": f["markerVerified"], "status": f["status"]}
            for f in manifest["fixes"]
        },
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_init(args) -> int:
    root = os.path.abspath(args.root)
    fixes_path, manifest_path, history_path = default_paths(root, args.dir)
    if not os.path.exists(fixes_path):
        os.makedirs(os.path.dirname(fixes_path), exist_ok=True)
        with open(fixes_path, "w", encoding="utf-8") as f:
            json.dump({"fixes": [{
                "id": "EXAMPLE-001",
                "desc": "Race in token refresh — early-return on in-flight refresh",
                "file": "src/auth.py",
                "marker": "if self._refreshing:",
            }]}, f, indent=2)
            f.write("\n")
        print(f"wrote template: {fixes_path}")
    return cmd_regen(args)


def cmd_regen(args) -> int:
    root = os.path.abspath(args.root)
    fixes_path, manifest_path, history_path = default_paths(root, args.dir)
    if args.fixes:
        fixes_path = os.path.abspath(args.fixes)
    if args.manifest:
        manifest_path = os.path.abspath(args.manifest)
    if args.history:
        history_path = os.path.abspath(args.history)

    prev_statuses = {}
    if os.path.exists(history_path):
        with open(history_path, encoding="utf-8") as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        if lines:
            for fid, st in lines[-1].get("fixes", {}).items():
                prev_statuses[fid] = st.get("sha256", "")

    fixes = load_fixes(fixes_path)
    for fx in fixes:
        fx["_prev_sha256"] = prev_statuses.get(fx["id"])
    scanned = scan_fixes(root, fixes)
    commit = git_commit(root, args.commit)
    manifest = build_manifest(root, commit, scanned)
    integrity = write_manifest(manifest_path, manifest)
    append_history(history_path, manifest)

    s = manifest["summary"]
    print(f"witness regen ({manifest['os']})")
    print("─" * 40)
    print(f"  gitCommit:    {commit[:12]}")
    print(f"  branch:       {manifest['branch']}")
    print(f"  total fixes:  {s['totalFixes']}  (verified={s['verified']} drift={s['drift']} regressed={s['regressed']} missing={s['missing']})")
    print(f"  manifestHash: {integrity['manifestHash'][:16]}…")
    print(f"written:  {manifest_path}")
    print(f"appended: {history_path}")
    return 1 if (s["regressed"] or s["missing"]) else 0


def cmd_verify(args) -> int:
    with open(args.manifest, encoding="utf-8") as f:
        doc = json.load(f)
    manifest, integrity = doc["manifest"], doc["integrity"]

    # manifest lives at <root>/verification/<os>/manifest.md.json
    root = os.path.abspath(args.root) if args.root else os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(args.manifest))))
    canon = canonical(manifest)
    digest = hashlib.sha256(canon).hexdigest()

    report = {
        "ok": True,
        "os": manifest.get("os"),
        "gitCommit": manifest.get("gitCommit"),
        "hashMatches": digest == integrity.get("manifestHash"),
        "signatureValid": _verify(
            bytes.fromhex(integrity.get("publicKey", "")),
            bytes.fromhex(integrity.get("signature", "")),
            canon,
        ),
        "statuses": {},
    }
    if not report["hashMatches"]:
        report["ok"] = False
        print("INTEGRITY: manifestHash mismatch — manifest was edited after signing")
    if report["signatureValid"] is None:
        print("WARNING: no Ed25519 backend (pip install cryptography); signature unchecked, hash still enforced")
    elif not report["signatureValid"]:
        report["ok"] = False
        print("INTEGRITY: Ed25519 signature invalid — manifest tampered or key derivation drift")

    for fx in manifest.get("fixes", []):
        path = os.path.join(root, fx["file"])
        cur = sha256_file(path)
        text = read_text_normalized(path)
        marker_ok = fix_holds(fx, text)
        if cur is None:
            status = "missing"
        elif not marker_ok:
            status = "regressed"
        elif cur != fx.get("sha256"):
            status = "drift"
        else:
            status = "verified"
        report["statuses"][fx["id"]] = status
        if status in ("regressed", "missing"):
            report["ok"] = False

    if args.fixes:
        listed = {fx["id"] for fx in load_fixes(args.fixes)}
        signed = {fx["id"] for fx in manifest.get("fixes", [])}
        report["unsigned"] = sorted(listed - signed)
        if report["unsigned"]:
            report["ok"] = False
            print(f"STALE: fixes listed but not in the signed manifest (run regen): {', '.join(report['unsigned'])}")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"witness verify ({manifest.get('os')} @ {manifest.get('gitCommit', '')[:12]})")
        print(f"  hash matches:            {'yes' if report['hashMatches'] else 'NO'}")
        print(f"  signature valid:         { {True: 'yes', False: 'NO', None: 'unchecked'}[report['signatureValid']] }")
        counts: dict[str, int] = {}
        for st in report["statuses"].values():
            counts[st] = counts.get(st, 0) + 1
        print(f"  summary: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "  summary: (no fixes)")
        reasons = {
            "regressed": "marker gone or forbidden pattern present",
            "missing": "file not found",
        }
        drifted = []
        for fid, st in report["statuses"].items():
            fix = next((f for f in manifest.get("fixes", []) if f["id"] == fid), {})
            if st in reasons:
                print(f"  {st.upper()}: {fid} — {fix.get('file', '?')} ({reasons[st]})")
            elif st == "drift":
                drifted.append(fid)
        if drifted:
            print(f"  drift (file edited, fix intact; informational): {', '.join(drifted)}")
    return 0 if report["ok"] else 1


def read_history(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def cmd_history(args) -> int:
    entries = read_history(args.history)
    if not entries:
        print("history is empty")
        return 0
    if args.cmd == "summary":
        print(f"{len(entries)} snapshots")
        first, last = entries[0], entries[-1]
        print(f"  first: {first['commit'][:12]}  {first['issuedAt']}")
        print(f"  last:  {last['commit'][:12]}  {last['issuedAt']}  {last['summary']}")
        ids = sorted({fid for e in entries for fid in e.get("fixes", {})})
        print(f"  tracked fixes: {len(ids)}")
        return 0

    bad = {"regressed", "missing"}
    if args.cmd == "regressions":
        found = False
        for fid in sorted({fid for e in entries for fid in e.get("fixes", {})}):
            last_pass = None
            for e in entries:
                st = e.get("fixes", {}).get(fid)
                if not st:
                    continue
                if st.get("status") in bad:
                    if last_pass:
                        found = True
                        print(f"{fid}")
                        print(f"  last pass:    {last_pass['commit'][:12]}  {last_pass['issuedAt']}")
                        print(f"  regressed at: {e['commit'][:12]}  {e['issuedAt']}")
                        print(f"  → git log {last_pass['commit'][:12]}..{e['commit'][:12]} collapses the bisect")
                    else:
                        found = True
                        print(f"{fid}: regressed at {e['commit'][:12]} ({e['issuedAt']}) with no prior passing snapshot")
                    break
                last_pass = e
        if not found:
            print("no regressions in history")
        return 0

    # timeline
    fid = args.id
    if not fid:
        print("--id required for timeline")
        return 2
    for e in entries:
        st = e.get("fixes", {}).get(fid)
        if st:
            print(f"{e['issuedAt']}  {e['commit'][:12]}  {st['status']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create fixes template + empty manifest/history")
    p.add_argument("--root", default=".")
    p.add_argument("--dir", default="verification", help="witness dir under root")
    p.add_argument("--commit", default=None)
    p.add_argument("--fixes", default=None)
    p.add_argument("--manifest", default=None)
    p.add_argument("--history", default=None)
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("regen", help="re-scan fixes, re-sign manifest, append history")
    p.add_argument("--root", default=".")
    p.add_argument("--dir", default="verification", help="witness dir under root")
    p.add_argument("--commit", default=None, help="override git commit (for CI reproducibility)")
    p.add_argument("--fixes", default=None)
    p.add_argument("--manifest", default=None)
    p.add_argument("--history", default=None)
    p.set_defaults(fn=cmd_regen)

    p = sub.add_parser("verify", help="verify manifest integrity + fix markers in the current tree")
    p.add_argument("--manifest", required=True)
    p.add_argument("--root", default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--fixes", default=None,
                   help="also fail if a fix in this list is absent from the manifest (forgot to regen)")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("history", help="inspect the JSONL history")
    p.add_argument("--history", required=True)
    p.add_argument("hcmd", choices=["summary", "regressions", "timeline"])
    p.add_argument("--id", default=None)
    p.set_defaults(fn=cmd_history)

    args = ap.parse_args(argv)
    if args.cmd == "history":
        args.cmd = args.hcmd
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
