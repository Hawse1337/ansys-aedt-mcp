# -*- coding: utf-8 -*-
"""Tests for the guard-generated builder approval request contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aedt_guard import approval_contract as contract
from aedt_guard.builders import sha256_file


def _setup(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    candidate = tmp_path / "p0_model.py"
    candidate.write_text("def run(**kwargs):\n    return {}\n", encoding="utf-8")
    allowlist = tmp_path / "builders.json"
    allowlist.write_text(
        json.dumps(
            {
                "version": 1,
                "builders": {
                    "p0_model": {
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
    monkeypatch.setenv("AEDT_GUARD_BUILDERS", str(allowlist))
    monkeypatch.setenv("AEDT_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AEDT_APPROVAL_REQUESTS_DIR", str(tmp_path / "requests"))
    return candidate, allowlist


def test_request_contract_computes_hashes_and_writes_request(tmp_path: Path, monkeypatch):
    candidate, _allowlist = _setup(tmp_path, monkeypatch)

    record, path = contract.build_request(
        "p0_model",
        str(candidate),
        "fix port integration line",
        request_type="builder_change",
        change_summary=["move integration line inside the half model"],
        new_params=[{"name": "symmetry", "type": "enum", "values": ["", "none", "x"]}],
        tested=["python -m py_compile candidate"],
    )

    assert path.is_file()
    assert record["candidate_sha256"] == sha256_file(candidate)
    assert record["old_approved_sha256"] == sha256_file(candidate)
    assert record["status"] == "pending"
    assert record["request_id"] in path.name
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["candidate"] == str(candidate.resolve())
    assert saved["new_params"][0]["name"] == "symmetry"
    sidecar = candidate.with_name(candidate.stem + ".approval-request.json")
    assert sidecar.is_file()
    assert contract.list_requests()[0]["request_id"] == record["request_id"]

    decided = contract.decide_request(
        record["request_id"], "approved", decided_by="operator", note="reviewed"
    )
    assert decided["status"] == "approved"
    assert not path.exists()
    assert contract.list_requests() == []


def test_request_contract_rejects_bad_candidate(tmp_path: Path, monkeypatch):
    _candidate, _allowlist = _setup(tmp_path, monkeypatch)
    with pytest.raises(contract.ApprovalRequestError):
        contract.build_request("p0_model", str(tmp_path / "missing.py"), "x")


def test_mcp_request_tool_writes_pending_request(tmp_path: Path, monkeypatch):
    import asyncio

    import mcp_server

    candidate, _allowlist = _setup(tmp_path, monkeypatch)
    result = asyncio.run(
        mcp_server.request_builder_approval(
            builder_id="p0_model",
            candidate_path=str(candidate),
            reason="fix half-model port",
            request_type="builder_change",
            new_params=[{"name": "symmetry", "type": "enum", "values": ["", "none", "x"]}],
            platform="Codex",
            session_id="session-test",
        )
    )
    assert result["refused"] is False
    assert result["status"] == "pending"
    assert Path(result["request_path"]).is_file()
    assert result["candidate_sha256"] == sha256_file(candidate)
