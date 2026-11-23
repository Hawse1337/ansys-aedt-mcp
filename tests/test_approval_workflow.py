# -*- coding: utf-8 -*-
"""Tests for request supersede, atomic schema approval and low-risk auto approval."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from aedt_guard import approval_contract as contract  # noqa: E402
from aedt_guard.builders import sha256_file  # noqa: E402
import approval_apply  # noqa: E402
import approval_inbox  # noqa: E402
import auto_approval  # noqa: E402


def _allowlist(tmp_path: Path, candidate: Path, monkeypatch) -> Path:
    path = tmp_path / "builders.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "builders": {
                    "demo": {
                        "script": str(candidate),
                        "sha256": sha256_file(candidate),
                        "entrypoint": "run",
                        "params": {"design": {"type": "string", "required": True}},
                        "output_roots": [str(tmp_path / "artifacts")],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AEDT_GUARD_BUILDERS", str(path))
    monkeypatch.setenv("AEDT_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AEDT_APPROVAL_REQUESTS_DIR", str(tmp_path / "requests"))
    return path


def _candidate(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_new_request_supersedes_old_pending(tmp_path: Path, monkeypatch):
    current = _candidate(tmp_path, "builder.py", "def run(design=None):\n    return design\n")
    _allowlist(tmp_path, current, monkeypatch)
    first = _candidate(
        tmp_path,
        "first.py",
        "def run(design=None):\n    # first attempt\n    return design\n",
    )
    second = _candidate(
        tmp_path,
        "second.py",
        "def run(design=None):\n    # newer\n    return design\n",
    )
    first_record, _ = contract.build_request("demo", str(first), "first attempt")
    second_record, _ = contract.build_request("demo", str(second), "replacement")
    assert first_record["request_id"] != second_record["request_id"]
    assert second_record["superseded_requests"] == [first_record["request_id"]]
    pending = contract.list_requests("pending")
    assert [row["request_id"] for row in pending] == [second_record["request_id"]]
    old, _ = contract.get_request(first_record["request_id"])
    assert old["status"] == "superseded"
    assert old["superseded_by"] == second_record["request_id"]
    try:
        contract.decide_request(first_record["request_id"], "approved")
    except contract.ApprovalRequestError:
        pass
    else:
        raise AssertionError("a superseded request must not be decidable")


def test_already_applied_candidate_is_not_a_new_pending_request(tmp_path: Path, monkeypatch):
    current = _candidate(tmp_path, "builder.py", "def run(design=None):\n    return design\n")
    _allowlist(tmp_path, current, monkeypatch)
    record, path = contract.build_request("demo", str(current), "already there")
    assert record["status"] == "approved"
    assert record["already_approved"] is True
    assert path.is_file()
    assert contract.list_requests("pending") == []


def test_atomic_approval_adds_schema_with_hash(tmp_path: Path, monkeypatch):
    current = _candidate(tmp_path, "builder.py", "def run(design=None):\n    return design\n")
    allowlist = _allowlist(tmp_path, current, monkeypatch)
    candidate = _candidate(
        tmp_path,
        "candidate.py",
        "def run(design=None, r_rim_mm=92.4):\n    return design, r_rim_mm\n",
    )
    record, _ = contract.build_request("demo", str(candidate), "add aperture", new_params=[{"name": "r_rim_mm", "type": "number", "min": 50, "max": 130}])
    pending = approval_inbox.list_pending()
    assert pending[0]["request_id"] == record["request_id"]
    result = approval_apply.apply_request(
        pending[0],
        allowlist_path=allowlist,
        approved_dir=tmp_path / "approved",
        decided_by="test",
    )
    assert result["approved"] is True
    data = json.loads(allowlist.read_text(encoding="utf-8"))
    entry = data["builders"]["demo"]
    assert entry["sha256"] == sha256_file(candidate)
    assert "r_rim_mm" in entry["params"]
    assert contract.list_requests("pending") == []
    archived = json.loads(
        (tmp_path / "requests" / "archive" / (record["request_id"] + ".json")).read_text(encoding="utf-8")
    )
    assert archived["status"] == "approved"
    assert archived["approved_sha256"] == sha256_file(candidate)


def test_low_risk_policy_accepts_small_existing_function_change(tmp_path: Path, monkeypatch):
    current = _candidate(
        tmp_path,
        "builder.py",
        "def run(design=None):\n    return design\n",
    )
    allowlist = _allowlist(tmp_path, current, monkeypatch)
    candidate = _candidate(
        tmp_path,
        "safe.py",
        "def run(design=None, r_rim_mm=92.4):\n    # profile\n    return design, r_rim_mm\n",
    )
    entry = json.loads(allowlist.read_text(encoding="utf-8"))["builders"]["demo"]
    request, _ = contract.build_request("demo", str(candidate), "safe change", new_params=[{"name": "r_rim_mm", "type": "number", "min": 50, "max": 130}])
    safe, reasons = auto_approval.low_risk(request, entry)
    assert safe, reasons
    report = auto_approval.auto_approve_pending(
        allowlist_path=allowlist, approved_dir=tmp_path / "approved"
    )
    assert [item["request_id"] for item in report["approved"]] == [request["request_id"]]
    data = json.loads(allowlist.read_text(encoding="utf-8"))
    assert data["builders"]["demo"]["sha256"] == sha256_file(candidate)


def test_low_risk_policy_rejects_new_import(tmp_path: Path, monkeypatch):
    current = _candidate(tmp_path, "builder.py", "def run(design=None):\n    return design\n")
    allowlist = _allowlist(tmp_path, current, monkeypatch)
    candidate = _candidate(
        tmp_path,
        "unsafe.py",
        "import os\n\ndef run(design=None):\n    return design\n",
    )
    entry = json.loads(allowlist.read_text(encoding="utf-8"))["builders"]["demo"]
    request, _ = contract.build_request("demo", str(candidate), "unsafe change")
    safe, reasons = auto_approval.low_risk(request, entry)
    assert safe is False
    assert any("imports" in reason for reason in reasons)
