#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Write approval-request metadata next to a candidate builder.

Agents or operators call this when requesting operator approval for a builder change; the GUI
reads the sidecar and shows platform/session/time instead of duplicating the file path.

Example:
  python tools/request_builder_approval.py \
    --builder p0_model \
    --candidate "C:\\...\\scripts\\p0_model.py" \
    --platform "DeepSeek Harness" \
    --session-id dc6fbbc1-f71a-49bc-af68-013a43a61e5b \
    --session-title "接续天线设计任务" \
    --reason "p0_model.py added gap feed mode; allowlist sha256 + feed enum need approval"
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--builder", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--platform", required=True, help="DeepSeek Harness / Codex / other")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--session-title", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--requested-at", default="")
    parser.add_argument("--session-archive", default="")
    args = parser.parse_args()

    candidate = Path(args.candidate).expanduser().resolve()
    if not candidate.is_file():
        raise SystemExit("candidate not found: %s" % candidate)
    requested_at = args.requested_at or datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    record = {
        "builder_id": args.builder,
        "platform": args.platform,
        "session_id": args.session_id,
        "session_title": args.session_title,
        "requested_at": requested_at,
        "reason": args.reason,
        "candidate": str(candidate),
        "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
    }
    if args.session_archive:
        record["source_archive"] = str(Path(args.session_archive).expanduser())
    target = candidate.with_name(candidate.stem + ".approval-request.json")
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote:", target)
    print("candidate_sha256:", record["candidate_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
