#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Operator helper for the approval workflow of ``run_approved_builder`` builders.

Usage (operator only):
  python approve_builder.py check p0_model
  python approve_builder.py review p0_model "<source p0_model.py>"
  python approve_builder.py apply  p0_model "<source p0_model.py>" --yes

``apply`` copies the reviewed source into a stable approved directory, recomputes the SHA-256,
and atomically updates builders.local.json. The params schema and output roots of the existing
entry are preserved. Run ``review`` first and inspect the diff; re-run ``apply`` whenever the
builder code changes.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

GUARD_DIR = Path(r"C:\ProgramData\ansys-aedt-mcp")
DEFAULT_ALLOWLIST = GUARD_DIR / "builders.local.json"
DEFAULT_APPROVED_DIR = GUARD_DIR / "approved"


def load(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit("allowlist not found: %s" % path)
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix="builders-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "review", "apply"])
    parser.add_argument("builder_id")
    parser.add_argument("source", nargs="?", help="new builder source (review/apply)")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    parser.add_argument("--approved-dir", default=str(DEFAULT_APPROVED_DIR))
    parser.add_argument("--yes", action="store_true", help="confirm apply")
    args = parser.parse_args()

    allowlist_path = Path(args.allowlist).expanduser()
    allowed = load(allowlist_path)
    entry = allowed.get("builders", {}).get(args.builder_id)
    if not isinstance(entry, dict):
        raise SystemExit("unknown builder: %s" % args.builder_id)

    current = Path(entry["script"]).expanduser()
    if args.action == "check":
        if not current.is_file():
            print("MISSING script:", current)
            return 1
        actual = sha256(current)
        ok = actual == entry["sha256"].lower()
        print("builder:", args.builder_id)
        print("script:", current)
        print("allowlist sha256:", entry["sha256"])
        print("actual   sha256:", actual)
        print("STATUS:", "OK (approved copy matches)" if ok else "MISMATCH (re-review and apply)")
        return 0 if ok else 1

    if not args.source:
        raise SystemExit("source is required for review/apply")
    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        raise SystemExit("source not found: %s" % source)

    if args.action == "review":
        print("builder:", args.builder_id)
        print("current approved:", current)
        print("candidate       :", source)
        print("current sha256  :", sha256(current) if current.is_file() else "(none)")
        print("candidate sha256:", sha256(source))
        print("\n--- diff current -> candidate ---")
        before = current.read_text(encoding="utf-8", errors="replace").splitlines() if current.is_file() else []
        after = source.read_text(encoding="utf-8", errors="replace").splitlines()
        diff = list(difflib.unified_diff(before, after, fromfile=str(current), tofile=str(source), lineterm=""))
        print("\n".join(diff) if diff else "(no textual difference)")
        print("\nChecklist: geometry/boundaries/ports, no project deletion, no extra AEDT launch, output paths;")
        print("then run: python approve_builder.py apply %s \"%s\" --yes" % (args.builder_id, source))
        return 0

    if not args.yes:
        raise SystemExit("refusing apply without --yes; review the diff first")
    approved_dir = Path(args.approved_dir).expanduser()
    approved_dir.mkdir(parents=True, exist_ok=True)
    target = approved_dir / (args.builder_id + Path(source).suffix if Path(source).suffix else args.builder_id + ".py")
    shutil.copy2(source, target)
    digest = sha256(target)
    old_script = entry.get("script")
    old_hash = entry.get("sha256")
    entry["script"] = str(target.resolve())
    entry["sha256"] = digest
    allowed["builders"][args.builder_id] = entry
    save(allowlist_path, allowed)
    print("approved:", args.builder_id)
    print("script  :", entry["script"])
    print("sha256  :", digest)
    print("previous:", old_script, old_hash)
    print("allowlist updated:", allowlist_path)
    print("verify with: python approve_builder.py check %s" % args.builder_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
