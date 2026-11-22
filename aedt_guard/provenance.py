# -*- coding: utf-8 -*-
"""Append-only provenance for approved-builder runs.

The guard cannot prove that a run came from the optimisation system unless the
caller supplies provenance. This log records who claims what: manual GUI/agent
runs default to ``source=manual``; the aster adapter sends
``source=aster``/``optimizer=aster`` plus campaign and candidate ids. The
approver GUI reads this file to show whether recent work actually went through
the optimisation system.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .managed import state_dir

_LOCK = threading.Lock()
_MAX_STRING = 512
_ALLOWED_PROVENANCE = {
    "campaign_id",
    "campaign_dir",
    "candidate_id",
    "purpose",
    "source",
    "optimizer",
    "run_id",
}


def provenance_path() -> Path:
    override = os.environ.get("AEDT_OPTIMIZATION_PROVENANCE", "").strip()
    if override:
        return Path(override).expanduser()
    return state_dir() / "optimization_runs.jsonl"


def _text(value: Any, limit: int = _MAX_STRING) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def record_run(
    *,
    builder_id: str,
    status: str,
    project: str = "",
    outdir: str = "",
    script_sha256: str = "",
    campaign_id: str = "",
    campaign_dir: str = "",
    candidate_id: str = "",
    purpose: str = "manual",
    source: str = "manual",
    optimizer: str = "",
    run_id: str = "",
    error: str = "",
    artifacts: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "builder_id": _text(builder_id),
        "campaign_id": _text(campaign_id),
        "campaign_dir": _text(campaign_dir, 2048),
        "candidate_id": _text(candidate_id),
        "purpose": _text(purpose or "manual"),
        "source": _text(source or "manual"),
        "optimizer": _text(optimizer),
        "project": _text(project, 2048),
        "outdir": _text(outdir, 2048),
        "script_sha256": _text(script_sha256, 64),
        "run_id": _text(run_id),
        "status": _text(status),
        "error": _text(error, 1000),
        "artifact_count": len(artifacts or []),
    }
    if artifacts:
        record["artifacts_sample"] = [_text(path, 2048) for path in artifacts[:10]]
    if extra:
        record["extra"] = {
            str(key): _text(value) if not isinstance(value, (int, float, bool)) else value
            for key, value in list(extra.items())[:20]
        }
    path = provenance_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _LOCK:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
    except OSError:
        pass
    return record


def read_recent(limit: int = 200) -> list[dict[str, Any]]:
    path = provenance_path()
    if not path.is_file():
        return []
    lines: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    lines.append(line)
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-max(1, int(limit)):]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    rows.reverse()  # newest first for the GUI/status consumers
    return rows


def latest() -> dict[str, Any] | None:
    rows = read_recent(1)
    return rows[0] if rows else None


def campaign_rows(campaign_id: str) -> list[dict[str, Any]]:
    if not campaign_id:
        return []
    return [row for row in read_recent(10000) if row.get("campaign_id") == campaign_id]


def _now() -> float:
    return time.time()
