# -*- coding: utf-8 -*-
"""Tests for the append-only optimization provenance log."""

from __future__ import annotations

from pathlib import Path

from aedt_guard import provenance


def test_record_and_read_provenance(tmp_path: Path, monkeypatch):
    target = tmp_path / "optimization_runs.jsonl"
    monkeypatch.setenv("AEDT_OPTIMIZATION_PROVENANCE", str(target))
    first = provenance.record_run(
        builder_id="p0_model",
        status="completed",
        campaign_id="c1",
        campaign_dir=str(tmp_path / "campaign"),
        candidate_id="candidate-1",
        purpose="candidate",
        source="aster",
        optimizer="aster",
        run_id="run-1",
        artifacts=[str(tmp_path / "result.s1p")],
    )
    provenance.record_run(
        builder_id="p0_model",
        status="completed",
        source="manual",
        purpose="diagnostic",
    )
    rows = provenance.read_recent(10)
    assert [row["source"] for row in rows] == ["manual", "aster"]
    assert rows[1]["campaign_id"] == "c1"
    assert rows[1]["optimizer"] == "aster"
    assert rows[1]["candidate_id"] == "candidate-1"
    assert first["run_id"] == "run-1"
    assert provenance.latest()["source"] == "manual"
    assert len(provenance.campaign_rows("c1")) == 1
