# -*- coding: utf-8 -*-
"""Provenance plumbing through the run_approved_builder MCP tool (no AEDT)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aedt_guard.builders import sha256_file


def _allowlist(tmp_path: Path) -> Path:
    script = tmp_path / "builder.py"
    script.write_text(
        "def run(port=None, project=None, outdir=None, theta_deg=0, family=None):\n"
        "    return {'port': port, 'project': project, 'outdir': outdir}\n",
        encoding="utf-8",
    )
    payload = {
        "version": 1,
        "builders": {
            "demo": {
                "script": str(script),
                "sha256": sha256_file(script),
                "entrypoint": "run",
                "params": {
                    "theta_deg": {"type": "number", "min": 0, "max": 60, "required": True},
                    "family": {"type": "enum", "values": ["A", "B"], "required": True},
                },
                "output_roots": [str(tmp_path / "artifacts")],
            }
        },
    }
    path = tmp_path / "builders.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_mcp_records_optimizer_provenance(tmp_path: Path, monkeypatch):
    import mcp_server

    allowlist = _allowlist(tmp_path)
    monkeypatch.setenv("AEDT_GUARD_BUILDERS", str(allowlist))
    monkeypatch.setenv("AEDT_OPTIMIZATION_PROVENANCE", str(tmp_path / "runs.jsonl"))
    project = tmp_path / "source.aedt"
    project.write_text("{}", encoding="utf-8")
    outdir = tmp_path / "artifacts"
    outdir.mkdir()

    async def fake_worker_call(command, **kwargs):
        return {"executed": True, "result": {"saved": str(outdir / "P0_demo.aedt")}}

    monkeypatch.setattr(mcp_server, "_worker_call", fake_worker_call)
    result = asyncio.run(
        mcp_server.run_approved_builder(
            builder_id="demo",
            project_path=str(project),
            outdir=str(outdir),
            params={"theta_deg": 20, "family": "A"},
            pid=1234,
            campaign_id="campaign-1",
            campaign_dir=str(tmp_path / "campaign"),
            candidate_id="candidate-1",
            purpose="candidate",
            source="aster",
            optimizer="aster",
        )
    )
    assert result["provenance"]["source"] == "aster"
    assert result["provenance"]["campaign_id"] == "campaign-1"
    rows = [
        json.loads(line)
        for line in (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[-1]["source"] == "aster"
    assert rows[-1]["campaign_id"] == "campaign-1"
    assert rows[-1]["candidate_id"] == "candidate-1"
