# -*- coding: utf-8 -*-
"""Tests for the GUI-side optimisation provenance reader."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import optimization_view  # noqa: E402


def test_reads_provenance_and_gate(tmp_path: Path, monkeypatch):
    provenance = tmp_path / "runs.jsonl"
    provenance.write_text(
        json.dumps({"source": "aster", "optimizer": "aster", "campaign_id": "c1",
                    "campaign_dir": str(tmp_path / "campaign"), "status": "completed"})
        + "\n"
        + json.dumps({"source": "manual", "status": "completed"})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AEDT_OPTIMIZATION_PROVENANCE", str(provenance))
    rows = optimization_view.read_recent(10)
    assert [row["source"] for row in rows] == ["manual", "aster"]
    assert optimization_view.is_optimizer_run(rows[1]) is True
    assert optimization_view.is_optimizer_run(rows[0]) is False

    campaign = tmp_path / "campaign"
    campaign.mkdir()
    (campaign / "optimization_gate.json").write_text(
        json.dumps({"passed": True, "missing": []}), encoding="utf-8"
    )
    assert optimization_view.gate_for_campaign(str(campaign))["passed"] is True
    assert optimization_view.gate_for_campaign(str(tmp_path / "missing"))["passed"] is False
