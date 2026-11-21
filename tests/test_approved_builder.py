# -*- coding: utf-8 -*-
"""Tests for the allowlisted builder capability (no AEDT required)."""

from __future__ import annotations

import json
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from aedt_guard.builders import (
    BuilderError,
    get_builder,
    load_allowlist,
    sha256_file,
    validate_output_dir,
    validate_params,
)
from aedt_target import AedtTarget
from pyaedt_capabilities import OfficialCapabilities


def _entry(tmp_path: Path, body: str = "def run(port=None, project=None, outdir=None, theta_deg=0, family=None, design=None):\n"
                                     "    return {'port': port, 'project': project, 'outdir': outdir, 'theta': theta_deg, 'family': family}\n"):
    project = tmp_path / "source.aedt"
    project.write_text("{}", encoding="utf-8")
    script = tmp_path / "builder.py"
    script.write_text(body, encoding="utf-8")
    return {
        "script": str(script),
        "sha256": sha256_file(script),
        "entrypoint": "run",
        "targets": [{"project_path": str(project), "project_sha256": sha256_file(project), "design": "D"}],
        "params": {
            "theta_deg": {"type": "number", "min": 0, "max": 60, "required": True},
            "family": {"type": "enum", "values": ["A", "B"], "required": True},
        },
        "output_roots": [str(tmp_path / "artifacts")],
    }


def _allowlist(entry):
    return {"version": 1, "builders": {"demo": entry}}


def test_allowlist_accepts_matching_hash_and_typed_params(tmp_path: Path):
    entry = _entry(tmp_path)
    allowlist = _allowlist(entry)
    builder = get_builder(allowlist, "demo")
    cleaned = validate_params(builder, {"theta_deg": 20, "family": "A"})
    assert cleaned == {"theta_deg": 20.0, "family": "A"}


def test_allowlist_rejects_changed_script(tmp_path: Path):
    entry = _entry(tmp_path)
    entry["sha256"] = "0" * 64
    with pytest.raises(BuilderError, match="hash changed"):
        get_builder(_allowlist(entry), "demo")


def test_params_reject_unknown_out_of_range_and_missing(tmp_path: Path):
    builder = get_builder(_allowlist(_entry(tmp_path)), "demo")
    with pytest.raises(BuilderError, match="not allowed"):
        validate_params(builder, {"theta_deg": 20, "family": "A", "evil": 1})
    with pytest.raises(BuilderError, match="above maximum"):
        validate_params(builder, {"theta_deg": 99, "family": "A"})
    with pytest.raises(BuilderError, match="missing required"):
        validate_params(builder, {"theta_deg": 20})
    with pytest.raises(BuilderError, match="one of"):
        validate_params(builder, {"theta_deg": 20, "family": "C"})


def test_output_dir_must_be_inside_allowed_roots(tmp_path: Path):
    entry = _entry(tmp_path)
    entry["output_roots"] = [str(tmp_path / "allowed")]
    allowed = validate_output_dir(entry, str(tmp_path / "allowed" / "run1"))
    assert allowed.endswith("run1")
    with pytest.raises(BuilderError, match="outside"):
        validate_output_dir(entry, str(tmp_path / "elsewhere"))


def test_backend_runs_approved_builder_without_arbitrary_code_gate(tmp_path: Path, monkeypatch):
    entry = _entry(tmp_path)
    allowlist = tmp_path / "builders.json"
    allowlist.write_text(json.dumps(_allowlist(entry)), encoding="utf-8")
    monkeypatch.setenv("AEDT_GUARD_BUILDERS", str(allowlist))
    project = tmp_path / "source.aedt"
    project.write_text("{}", encoding="utf-8")
    outdir = tmp_path / "artifacts"
    outdir.mkdir()
    desktop = SimpleNamespace(
        port=5555,
        odesktop=object(),
        project_file=None,
        active_project_name=None,
    )
    from aedt_guard.builders import prepare_request, request_id
    session = {"kind": "port", "value": 5555}
    _, request, _ = prepare_request("demo", str(project), str(outdir),
                                    {"theta_deg": 20, "family": "A", "design": "D"}, session)
    work = outdir / "_work" / "test" / "source-copy.aedt"
    work.parent.mkdir(parents=True)
    work.write_bytes(project.read_bytes())
    result = OfficialCapabilities()._run_approved_builder(
        desktop,
        AedtTarget(kind="port", value=5555),
        {
            "builder_id": "demo",
            "params": {"theta_deg": 20, "family": "A", "design": "D"},
            "project_path": str(work), "source_project": str(project),
            "requested_session": session, "request_id": request_id(request),
            "outdir": str(outdir),
            # Caller-supplied execution fields must be ignored; the worker loads the allowlist.
            "script_path": str(tmp_path / "evil.py"),
            "script_sha256": "0" * 64,
            "entrypoint": "evil",
        },
        {},
    )
    assert result["executed"] is True
    assert result["result"]["port"] == 5555
    assert result["result"]["theta"] == 20.0
    assert str(work) == result["result"]["project"]


def test_mcp_tool_refuses_when_hash_changed(tmp_path: Path, monkeypatch):
    import asyncio

    import mcp_server

    entry = _entry(tmp_path)
    allowlist = tmp_path / "builders.json"
    allowlist.write_text(json.dumps(_allowlist(entry)), encoding="utf-8")
    monkeypatch.setenv("AEDT_GUARD_BUILDERS", str(allowlist))
    project = tmp_path / "source.aedt"
    project.write_text("{}", encoding="utf-8")
    outdir = tmp_path / "artifacts"
    outdir.mkdir()
    Path(entry["script"]).write_text("def run(**kwargs):\n    return {}\n", encoding="utf-8")
    result = asyncio.run(
        mcp_server.run_approved_builder(
            builder_id="demo",
            project_path=str(project),
            outdir=str(outdir),
            params={"theta_deg": 20, "family": "A", "design": "D"},
        )
    )
    assert result["refused"] is True
    assert "hash changed" in result["reason"]

def test_mcp_tool_success_path_uses_unique_copy_and_cleans_work(tmp_path: Path, monkeypatch):
    import asyncio

    import mcp_server

    entry = _entry(tmp_path)
    allowlist = tmp_path / "builders.json"
    allowlist.write_text(json.dumps(_allowlist(entry)), encoding="utf-8")
    monkeypatch.setenv("AEDT_GUARD_BUILDERS", str(allowlist))
    project = tmp_path / "source.aedt"
    project.write_text("{}", encoding="utf-8")
    outdir = tmp_path / "artifacts"
    outdir.mkdir()
    captured = {}

    async def fake_worker_call(command, **kwargs):
        captured.update({"command": command, **kwargs})
        return {"executed": True, "result": {"saved": str(outdir / "P0_demo.aedt")}}

    monkeypatch.setattr(mcp_server, "_worker_call", fake_worker_call)
    result = asyncio.run(
        mcp_server.run_approved_builder(
            builder_id="demo",
            project_path=str(project),
            outdir=str(outdir),
            params={"theta_deg": 20, "family": "A", "design": "D"},
            pid=1234,
        )
    )
    assert result["executed"] is True
    assert captured["command"] == "run_approved_builder"
    args = captured["arguments"]
    assert args["builder_id"] == "demo"
    assert args["params"] == {"theta_deg": 20.0, "family": "A", "design": "D"}
    assert "p0work_" in Path(args["project_path"]).name
    assert project.read_text(encoding="utf-8") == "{}"
    assert not any((outdir / "_work").rglob("*.aedt")) if (outdir / "_work").exists() else True
    assert result["working_project"] == args["project_path"]
    assert result["requested_project"] == str(project.resolve())


def test_mcp_tool_refusal_is_audited(tmp_path: Path, monkeypatch):
    import asyncio

    import mcp_server
    from aedt_guard.managed import state_dir

    entry = _entry(tmp_path)
    allowlist = tmp_path / "builders.json"
    allowlist.write_text(json.dumps(_allowlist(entry)), encoding="utf-8")
    monkeypatch.setenv("AEDT_GUARD_BUILDERS", str(allowlist))
    project = tmp_path / "source.aedt"
    project.write_text("{}", encoding="utf-8")
    outdir = tmp_path / "artifacts"
    outdir.mkdir()
    result = asyncio.run(
        mcp_server.run_approved_builder(
            builder_id="demo",
            project_path=str(project),
            outdir=str(outdir),
            params={"theta_deg": 20, "family": "Z", "design": "D"},
            pid=1234,
        )
    )
    assert result["refused"] is True
    audit = state_dir() / "guard_audit.jsonl"
    lines = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(
        line.get("command") == "run_approved_builder"
        and line.get("outcome") == "refused"
        and "allowlist/params/path refused" in line.get("detail", "")
        for line in lines
    )

def test_reserved_parameter_names_are_rejected(tmp_path: Path):
    builder = get_builder(_allowlist(_entry(tmp_path)), "demo")
    for name in ("port", "project", "outdir"):
        with pytest.raises(BuilderError, match="reserved"):
            validate_params(builder, {"theta_deg": 20, "family": "A", name: 1})


def test_guard_status_reports_builder_allowlist(tmp_path: Path, monkeypatch):
    import asyncio

    import mcp_server

    entry = _entry(tmp_path)
    allowlist = tmp_path / "builders.json"
    allowlist.write_text(json.dumps(_allowlist(entry)), encoding="utf-8")
    monkeypatch.setenv("AEDT_GUARD_BUILDERS", str(allowlist))
    monkeypatch.setattr(
        mcp_server.managed_sessions,
        "scan_aedt_processes",
        lambda prune_dead=True: {"managed": [], "unmanaged": [], "enforcement": "managed_only"},
    )
    monkeypatch.setattr(mcp_server.managed_sessions, "list_managed", lambda prune_dead=True: [])
    status = asyncio.run(mcp_server.guard_status())
    assert status["builders"]["exists"] is True
    assert "demo" in status["builders"]["ids"]
    assert "same-user" in status["builders"]["warning"]
    assert status["builders"]["scripts"][0]["builder_id"] == "demo"

def test_scope_and_operator_one_time_approval(tmp_path, monkeypatch):
    import asyncio
    import sys
    import mcp_server
    from scripts import approve_builder_request as operator

    entry = _entry(tmp_path)
    policy = tmp_path / "builders.json"
    policy.write_text(json.dumps(_allowlist(entry)), encoding="utf-8")
    monkeypatch.setenv("AEDT_GUARD_BUILDERS", str(policy))
    monkeypatch.setenv("AEDT_MCP_STATE_DIR", str(tmp_path / "guard_state"))
    source = tmp_path / "source.aedt"
    outdir = tmp_path / "artifacts"
    calls = []

    async def worker(command, **kwargs):
        calls.append(command)
        return OfficialCapabilities()._run_approved_builder(
            SimpleNamespace(port=5555), AedtTarget("port", 5555), kwargs["arguments"], {})

    monkeypatch.setattr(mcp_server, "_worker_call", worker)

    def invoke(theta=70, design="D", port=5555):
        return asyncio.run(mcp_server.run_approved_builder(
            "demo", str(source), str(outdir), {"theta_deg": theta, "family": "A", "design": design},
            port=port))

    pending = invoke()
    assert pending["status"] == "approval_required" and pending["approval_mode"] == "once"
    assert pending["request"]["project_sha256"] == sha256_file(source)
    assert calls == [] and not outdir.exists()
    review = tmp_path / "request.json"
    review.write_text(json.dumps(pending), encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "approve " + pending["request_id"][:12])
    operator.main([str(review)])
    for kwargs in [{"theta": 71}, {"port": 5556}, {"design": "Different"}]:
        other = invoke(**kwargs)
        assert other["status"] == "approval_required"
        assert other["request_id"] != pending["request_id"]
    source.write_text("changed", encoding="utf-8")
    assert invoke()["status"] == "approval_required"
    source.write_text("{}", encoding="utf-8")
    assert not calls
    assert invoke()["executed"] is True
    assert invoke()["status"] == "approval_required"
    assert calls == ["run_approved_builder"]
    assert invoke(theta=20)["executed"] is True


def test_one_approval_has_only_one_concurrent_claim(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from aedt_guard.builders import approval_directory, approved_once, prepare_request, request_id
    from aedt_guard.managed import state_dir

    entry = _entry(tmp_path)
    policy = tmp_path / "builders.json"
    policy.write_text(json.dumps(_allowlist(entry)), encoding="utf-8")
    monkeypatch.setenv("AEDT_GUARD_BUILDERS", str(policy))
    monkeypatch.setenv("AEDT_MCP_STATE_DIR", str(tmp_path / "guard_state"))
    _, request, deviations = prepare_request("demo", str(tmp_path / "source.aedt"),
        str(tmp_path / "artifacts"), {"theta_deg": 70, "family": "A", "design": "D"},
        {"kind": "managed_launch"})
    assert deviations
    directory = approval_directory()
    directory.mkdir()
    digest = request_id(request)
    grant = {"request_id": digest, "nonce": "a" * 32, "ledger_root": str(state_dir().resolve())}
    (directory / (digest + ".json")).write_text(json.dumps(grant), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=5) as pool:
        claims = list(pool.map(lambda _: approved_once(request, consume=True), range(8)))
    assert claims.count(True) == 1


def test_legacy_allowlist_requires_target_migration(tmp_path, monkeypatch):
    import asyncio
    import mcp_server

    entry = _entry(tmp_path)
    del entry["targets"]
    policy = tmp_path / "builders.json"
    policy.write_text(json.dumps(_allowlist(entry)), encoding="utf-8")
    monkeypatch.setenv("AEDT_GUARD_BUILDERS", str(policy))
    result = asyncio.run(mcp_server.run_approved_builder(
        "demo", str(tmp_path / "source.aedt"), str(tmp_path / "artifacts"),
        {"theta_deg": 20, "family": "A", "design": "D"}))
    assert result["approval_required"] and result["executed"] is False
    assert result["approval_mode"] == "review_policy_or_request"
    assert "targets" in result["reason"]
