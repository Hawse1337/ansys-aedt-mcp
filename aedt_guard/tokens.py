"""Run tokens for the enforcement layer (P3).

A token authorises exactly one (project, design, setup) analysis for one run.
It is a small JSON file inside the campaign directory, signed with an HMAC key
that also lives in the campaign directory. This is not a security boundary
against a hostile local user (who could forge everything); it is a boundary
against silent workflow bypass while the campaign directory stays under the
tool's control.

Layout::

    <campaign>/.guard_key                      # 32-byte hex secret
    <campaign>/runs/<run_id>/run_token.json    # {"payload": {...}, "sig": "..."}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class TokenError(RuntimeError):
    """Raised when a run token is missing, invalid, expired or mismatched."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def campaign_key(campaign_dir: str | Path) -> bytes:
    """Read or create the campaign HMAC key."""
    root = Path(campaign_dir)
    key_path = root / ".guard_key"
    if key_path.is_file():
        return key_path.read_text(encoding="utf-8").strip().encode("utf-8")
    root.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    key_path.write_text(key, encoding="utf-8")
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key.encode("utf-8")


def campaign_root_for_token(token_path: str | Path) -> Path:
    """``<campaign>/runs/<run_id>/run_token.json`` -> ``<campaign>``."""
    path = Path(token_path).expanduser().resolve()
    if path.name != "run_token.json" or path.parent.parent.parent == path.parent:
        raise TokenError(f"not a run token path: {path}")
    root = path.parent.parent.parent
    if not (root / ".guard_key").is_file() and not root.name:
        raise TokenError(f"cannot locate campaign root for {path}")
    return root


def issue_token(
    campaign_dir: str | Path,
    *,
    run_id: str,
    project: str,
    design: str,
    setup: str,
    ttl_s: float = 7200.0,
) -> Path:
    root = Path(campaign_dir)
    payload = {
        "run_id": str(run_id),
        "project": str(project),
        "design": str(design),
        "setup": str(setup),
        "issued_utc": _now().isoformat(),
        "expires_utc": (_now() + timedelta(seconds=float(ttl_s))).isoformat(),
    }
    signature = hmac.new(campaign_key(root), _canonical(payload), hashlib.sha256).hexdigest()
    token_path = root / "runs" / str(run_id) / "run_token.json"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        json.dumps({"payload": payload, "sig": signature}, indent=2),
        encoding="utf-8",
    )
    return token_path


def read_token(token_path: str | Path) -> dict[str, Any]:
    """Validate signature and expiry; return the payload."""
    path = Path(token_path).expanduser().resolve()
    if not path.is_file():
        raise TokenError(f"run token not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        payload = document["payload"]
        signature = document["sig"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise TokenError(f"run token is malformed: {path}") from exc
    root = campaign_root_for_token(path)
    expected = hmac.new(campaign_key(root), _canonical(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(signature)):
        raise TokenError("run token signature does not match the campaign key")
    expires = payload.get("expires_utc")
    try:
        expires_at = datetime.fromisoformat(str(expires))
    except ValueError as exc:
        raise TokenError("run token has an invalid expiry") from exc
    if expires_at <= _now():
        raise TokenError(f"run token expired at {expires}")
    return payload


def verify_token(
    token_path: str | Path,
    *,
    project: str | None = None,
    design: str | None = None,
    setup: str | None = None,
) -> dict[str, Any]:
    """Validate signature, expiry and identity of the requested analysis."""
    payload = read_token(token_path)
    checks = (("project", project), ("design", design), ("setup", setup))
    for field, requested in checks:
        if requested is None and payload.get(field) is not None:
            raise TokenError(
                f"run token is scoped to {field}='{payload.get(field)}'; the caller must "
                f"present the same {field} explicitly (omitting it is not a wildcard)"
            )
        if requested is not None and str(payload.get(field)) != str(requested):
            raise TokenError(
                f"run token authorises {field}='{payload.get(field)}', "
                f"not '{requested}'"
            )
    return payload


def revoke_token(token_path: str | Path) -> bool:
    path = Path(token_path).expanduser().resolve()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
