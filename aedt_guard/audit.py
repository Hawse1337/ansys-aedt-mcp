"""Audit trail and enforcement decisions for analysis submissions (P3).

Every attempt to start an analysis through the broker is recorded, whether it
was allowed or refused, so silent bypasses become visible in
``campaign_context`` as ``unguarded_count``.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .tokens import TokenError, campaign_root_for_token, verify_token

AUDIT_RELATIVE = Path("audit") / "analyze_audit.jsonl"

_TRUE = {"1", "true", "yes", "on"}


def enforcement_enabled() -> bool:
    return os.environ.get("AEDT_GUARD_ENFORCE", "").strip().lower() in _TRUE


def unguarded_allowed() -> bool:
    return os.environ.get("AEDT_ALLOW_UNGUARDED", "").strip().lower() in _TRUE


def _campaign_dir(arguments: dict[str, Any] | None) -> Path | None:
    token = (arguments or {}).get("run_token")
    if token:
        try:
            path = Path(token).expanduser().resolve()
            if not path.is_file():
                return None
            return campaign_root_for_token(token)
        except TokenError:
            return None
    root = os.environ.get("AEDT_CAMPAIGN_ROOT")
    if root:
        return Path(root).expanduser()
    return None


def append_audit(campaign_dir: Path, entry: dict[str, Any]) -> None:
    target = Path(campaign_dir) / AUDIT_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_audit(campaign_dir: str | Path) -> list[dict[str, Any]]:
    target = Path(campaign_dir) / AUDIT_RELATIVE
    if not target.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def summarize(campaign_dir: str | Path) -> dict[str, Any]:
    target = Path(campaign_dir) / AUDIT_RELATIVE
    raw_lines = 0
    malformed = 0
    if target.is_file():
        raw_lines = len([line for line in target.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()])
    entries = read_audit(campaign_dir)
    malformed = max(0, raw_lines - len(entries))
    denied = [e for e in entries if not e.get("allowed")]
    bypass = [e for e in entries if e.get("allowed") and e.get("reason") == "escape_hatch"]
    governed = [e for e in entries if e.get("allowed") and e.get("run_id")]
    return {
        "audit_entries": len(entries),
        "denied": len(denied),
        "unguarded_count": len(denied) + len(bypass),
        "escape_hatch": len(bypass),
        "governed": len(governed),
        "audit_present": target.is_file(),
        "malformed_lines": malformed,
        "tampered": malformed > 0,
    }


def _record(
    command: str,
    arguments: dict[str, Any],
    decision: dict[str, Any],
    *,
    project: str | None,
    design: str | None,
    setup: str | None,
) -> dict[str, Any]:
    campaign = _campaign_dir(arguments)
    if campaign is not None:
        append_audit(
            campaign,
            {
                "at_utc": datetime.now(timezone.utc).isoformat(),
                "command": command,
                "project": project,
                "design": design,
                "setup": setup,
                "token_present": bool(arguments.get("run_token")),
                "allowed": decision["allowed"],
                "reason": decision["reason"],
                "run_id": decision["run_id"],
            },
        )
    return decision


def _decide(
    command: str,
    arguments: dict[str, Any],
    *,
    project: str | None,
    design: str | None,
    setup: str | None,
) -> dict[str, Any]:
    token = arguments.get("run_token")
    if token:
        try:
            payload = verify_token(token, project=project, design=design, setup=setup)
        except TokenError as exc:
            return _record(
                command,
                arguments,
                {"allowed": False, "reason": f"invalid_run_token: {exc}", "run_id": None},
                project=project,
                design=design,
                setup=setup,
            )
        return _record(
            command,
            arguments,
            {"allowed": True, "reason": "run_token", "run_id": payload.get("run_id")},
            project=project,
            design=design,
            setup=setup,
        )
    if unguarded_allowed():
        return _record(
            command,
            arguments,
            {"allowed": True, "reason": "escape_hatch", "run_id": None},
            project=project,
            design=design,
            setup=setup,
        )
    return _record(
        command,
        arguments,
        {
            "allowed": False,
            "reason": (
                "missing run_token; submit through submit_guarded_run "
                "(AEDT_ALLOW_UNGUARDED=1 allows a logged bypass)"
            ),
            "run_id": None,
        },
        project=project,
        design=design,
        setup=setup,
    )


def enforce_analyze(
    command: str,
    arguments: dict[str, Any] | None,
    *,
    project: str | None = None,
    design: str | None = None,
    setup: str | None = None,
) -> dict[str, Any]:
    """Decide whether an analysis attempt may proceed, and audit it."""
    arguments = arguments or {}
    if not enforcement_enabled():
        return {"allowed": True, "reason": "enforcement_off", "run_id": None}
    return _decide(command, arguments, project=project, design=design, setup=setup)


def enforce_python_code(
    code: str,
    arguments: dict[str, Any] | None,
    *,
    project: str | None = None,
    design: str | None = None,
) -> dict[str, Any]:
    """Block free-form code that would start a solve without a run token.

    The scan is deliberately textual: any mention of ``Analyze`` (including the
    native ``odesign.Analyze`` spelling) requires a valid token. This catches
    accidental bypass; it is not a sandbox.
    """
    arguments = arguments or {}
    if not enforcement_enabled():
        return {"allowed": True, "reason": "enforcement_off", "run_id": None}
    if "analyze" not in str(code or "").lower():
        return {"allowed": True, "reason": "no_analyze_code", "run_id": None}
    return _decide(
        "run_python_code", arguments, project=project, design=design, setup=None
    )
