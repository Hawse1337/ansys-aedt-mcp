"""Guard-side test: a swept run must not report a bandwidth from one row."""
from __future__ import annotations

import json
import tempfile
import inspect
import unittest
from pathlib import Path

from aedt_guard.sim import CampaignRunner


class _Adapter:
    sweep_name = "ANT_Band"

    def __init__(self, rows: int):
        self.rows = rows
        self.calls: list[str] = []

    def finalize(self):
        return {"saved": True, "setup_solution_data": {"P4": True},
                "incomplete_setups": [], "solution_data_complete": True}

    def export_farfield(self, output_dir, cuts, frequencies=None):
        self.calls.append("farfield")
        return {"cuts": {}}

    def export_convergence(self, path):
        return {"evidence": {"assessment": "converged"}}

    def export_touchstone(self, output_file):
        self.calls.append("touchstone")
        return {"path": str(output_file),
                "details": {"row_count": self.rows, "attempts": 2}}


class _Store:
    def __init__(self):
        self.status = None

    def set_status(self, run_id, status, notes=None):
        self.status = status

    def add_metrics(self, run_id, metrics, extra=None):
        self.metrics = metrics


def _spec():
    return {
        "run_id": "run",
        "frequency_ghz": [24.0, 30.0],
        "evaluation": {"frequencies_ghz": [28.0]},
        "metrics": [{"id": "s11", "kind": "s11", "operator": "<=", "target": -10.0,
                     "hard": True, "band_ghz": [24.0, 30.0]}],
        "execution_policy": {},
    }


class SweepExportGuardTests(unittest.TestCase):
    def _run(self, rows: int):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        adapter = _Adapter(rows)
        store = _Store()
        runner = CampaignRunner(store=store, adapter=adapter, spec=_spec(),
                                run_id="run", artifact_dir=Path(tmp.name),
                                sleep=lambda _s: None)
        status = runner._finish()
        return status, json.dumps(runner.stage_log), store

    def test_single_row_touchstone_fails_closed(self):
        status, stage_notes, store = self._run(1)
        self.assertNotEqual(status, "COMPLETED")
        self.assertIn("E_SWEEP_EXPORT_INCOMPLETE", stage_notes)

    def test_three_rows_are_accepted(self):
        status, stage_notes, store = self._run(3)
        self.assertNotIn("E_SWEEP_EXPORT_INCOMPLETE", stage_notes)


if __name__ == "__main__":
    unittest.main()


class ShortTouchstoneRetryTests(unittest.TestCase):
    def test_a_short_touchstone_is_retried_when_the_policy_asks(self):
        """A sweep that is still solving exports one row; the retry must wait for it."""
        from aedt_guard.sim import CampaignRunner

        class Adapter:
            def __init__(self):
                self.calls = 0
                self.sweep_name = "ANT_Band"

            def export_touchstone(self, path):
                self.calls += 1
                rows = 1 if self.calls == 1 else 7
                return {"path": str(path), "details": {"row_count": rows,
                                                        "expected_rows": 7}}

        source = inspect.getsource(CampaignRunner)
        self.assertIn("touchstone_retries", source)
        self.assertIn("touchstone_retry_delay_s", source)
        # default must stay 1 so existing behaviour and tests are unchanged
        self.assertIn('policy.get("touchstone_retries", 1)', source)
