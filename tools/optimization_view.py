# -*- coding: utf-8 -*-
"""Read optimisation provenance for the approver GUI.

The guard appends one JSON line per approved-builder run. The GUI shows whether
the recent work came from aster (source=aster / optimizer=aster) or from a
manual run, and whether the campaign passed the completion gate.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MANIFEST_NAME = "optimization_manifest.json"
GATE_NAME = "optimization_gate.json"


def state_dir() -> Path:
    override = os.environ.get("AEDT_MCP_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "ansys-aedt-mcp"


def provenance_path() -> Path:
    override = os.environ.get("AEDT_OPTIMIZATION_PROVENANCE", "").strip()
    if override:
        return Path(override).expanduser()
    return state_dir() / "optimization_runs.jsonl"


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
    rows.reverse()
    return rows


def is_optimizer_run(row: dict[str, Any]) -> bool:
    return (
        str(row.get("source") or "").lower() in {"aster", "optimizer"}
        or str(row.get("optimizer") or "").lower() == "aster"
    )


def gate_for_campaign(campaign_dir: str) -> dict[str, Any]:
    """Read a gate written by antopt.optimization_gate; never raises."""
    if not campaign_dir:
        return {"available": False, "passed": False, "missing": ["campaign_dir"]}
    root = Path(campaign_dir)
    gate_path = root / GATE_NAME
    if gate_path.is_file():
        try:
            data = json.loads(gate_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("available", True)
                return data
        except (OSError, ValueError):
            pass
    manifest_path = root / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False, "passed": False, "missing": ["optimization_gate.json"]}
    checks = {
        "manifest": True,
        "optimizer": str(manifest.get("optimizer") or "").lower() == "aster",
        "source": str(manifest.get("source") or "").lower() in {"aster", "optimizer"},
        "completed_runs": any(
            str(row.get("status") or "").upper() == "COMPLETED"
            for row in (manifest.get("runs") or [])
            if isinstance(row, dict)
        ),
        "pareto": bool(manifest.get("pareto")),
        "acceptance": bool(manifest.get("acceptance"))
        and (
            not isinstance(manifest.get("acceptance"), dict)
            or bool(manifest["acceptance"].get("passed"))
        ),
    }
    missing = [name for name, passed in checks.items() if not passed]
    return {
        "available": True,
        "passed": all(checks.values()),
        "checks": checks,
        "missing": missing,
        "source": "manifest",
    }
