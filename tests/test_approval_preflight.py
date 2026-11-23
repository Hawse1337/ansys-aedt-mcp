# -*- coding: utf-8 -*-
"""Tests for server-side approval preflight and single-call waiting."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aedt_guard import approval_contract as contract
from aedt_guard.builders import sha256_file


def _setup(tmp_path: Path, monkeypatch, candidate_body: str):
    current = tmp_path / "builder.py"
    current.write_text("def run(**kwargs):\n    return {}\n", encoding="utf-8")
    allowlist = tmp_path / "builders.json"
    allowlist.write_text(
        json.dumps(
            {
                "version": 1,
                "builders": {
                    "demo": {
                        "script": str(current),
                        "sha256": sha256_file(current),
                        "entrypoint": "run",
                        "params": {"design": {"type": "string", "required": True}},
                        "output_roots": [str(tmp_path / "artifacts")],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AEDT_GUARD_BUILDERS", str(allowlist))
    monkeypatch.setenv("AEDT_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AEDT_APPROVAL_REQUESTS_DIR", str(tmp_path / "requests"))
    candidate = tmp_path / "candidate.py"
    candidate.write_text(candidate_body, encoding="utf-8")
    return allowlist, candidate


def test_preflight_rejects_undeclared_run_argument(tmp_path: Path, monkeypatch):
    import mcp_server

    _allow, candidate = _setup(
        tmp_path,
        monkeypatch,
        "def run(design=None, r_rim_mm=92.4):\n    return design, r_rim_mm\n",
    )
    result = asyncio.run(
        mcp_server.request_builder_approval(
            builder_id="demo",
            candidate_path=str(candidate),
            reason="add parameter",
            change_summary=["add r_rim"],
        )
    )
    assert result["refused"] is True
    assert result["status"] == "preflight_failed"
    assert "r_rim_mm" in result["reason"]
    assert contract.list_requests("pending") == []


def test_preflight_accepts_declared_schema_and_records_report(tmp_path: Path, monkeypatch):
    import mcp_server

    _allow, candidate = _setup(
        tmp_path,
        monkeypatch,
        "def run(design=None, r_rim_mm=92.4):\n    return design, r_rim_mm\n",
    )
    result = asyncio.run(
        mcp_server.request_builder_approval(
            builder_id="demo",
            candidate_path=str(candidate),
            reason="add parameter",
            new_params=[{"name": "r_rim_mm", "type": "number", "min": 50, "max": 130}],
            tested=["py_compile", "dry-run"],
        )
    )
    assert result["refused"] is False
    assert result["status"] == "pending"
    assert result["preflight"]["ok"] is True
    assert result["preflight"]["checks"]["schema_complete"] is True


def test_approval_budget_blocks_incremental_requests(tmp_path: Path, monkeypatch):
    allowlist_path, candidate = _setup(
        tmp_path,
        monkeypatch,
        "def run(design=None, extra=1):\n    return design, extra\n",
    )
    data = json.loads(allowlist_path.read_text(encoding="utf-8"))
    data["builders"]["demo"]["approval_budget_per_day"] = 1
    allowlist_path.write_text(json.dumps(data), encoding="utf-8")
    first = contract.build_request(
        "demo", str(candidate), "first",
        new_params=[{"name": "extra", "type": "number"}],
    )
    assert first[0]["status"] == "pending"
    second = tmp_path / "candidate2.py"
    second.write_text("def run(design=None, extra=1, other=2):\n    return design, extra, other\n", encoding="utf-8")
    try:
        contract.build_request(
            "demo", str(second), "second",
            new_params=[
                {"name": "extra", "type": "number"},
                {"name": "other", "type": "number"},
            ],
        )
    except contract.ApprovalRequestError as exc:
        assert "approval budget exceeded" in str(exc)
    else:
        raise AssertionError("budget should reject the second request")


def test_wait_for_approval_returns_terminal_status(tmp_path: Path, monkeypatch):
    import mcp_server

    allowlist, candidate = _setup(
        tmp_path,
        monkeypatch,
        "def run(design=None, extra=1):\n    return design, extra\n",
    )
    result = asyncio.run(
        mcp_server.request_builder_approval(
            builder_id="demo",
            candidate_path=str(candidate),
            reason="schema addition",
            new_params=[{"name": "extra", "type": "number"}],
        )
    )
    assert result["status"] == "pending"
    pending = asyncio.run(
        mcp_server.wait_for_builder_approval(result["request_id"], timeout_s=0.1)
    )
    assert pending["status"] == "pending"
    assert pending["timed_out"] is True
    contract.decide_request(result["request_id"], "approved", decided_by="test")
    done = asyncio.run(
        mcp_server.wait_for_builder_approval(result["request_id"], timeout_s=1.0)
    )
    assert done["status"] == "approved"
    assert done["terminal"] is True
    assert done["next_step"] == "retry_run_approved_builder"
