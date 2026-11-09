# -*- coding: utf-8 -*-
"""Offline tests for P4 items 2 and 3: cache hits, archive/Pareto, convergence gate."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import mcp_server
from aedt_guard import archive
from aedt_guard.sim import FAILED, CampaignRunner
from aedt_guard.store import Store


def _write_s1p(path: Path, level_db=-15.0) -> None:
    magnitude = 10 ** (level_db / 20.0)
    lines = ["# GHz S MA R 50"]
    for freq in np.arange(24.0, 30.01, 0.25):
        lines.append(f"{freq:.4f} {magnitude:.6f} 0.0")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_theta(path: Path, peak_gain=6.5) -> None:
    lines = ['"Theta [deg]","Phi [deg]","dB(GainTotal) []"']
    for theta in range(0, 181, 2):
        delta = abs(theta - 90.0)
        gain = peak_gain - 1.5 * delta if delta <= 12 else peak_gain - 18.0
        lines.append(f'{theta},"0.0",{gain:.4f}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_phi(path: Path) -> None:
    lines = ['"Phi [deg]","Theta [deg]","dB(GainTotal) []"']
    for phi in range(0, 361, 5):
        lines.append(f'{phi},"90.0",{6.0 if phi % 20 else 7.0:.4f}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _metrics(gain_target=4.0):
    return [
        {"id": "s11", "kind": "s11", "unit": "dB", "operator": "<=", "target": -10.0,
         "hard": True, "band_ghz": [24.0, 30.0]},
        {"id": "gain_h", "kind": "realized_gain_h", "unit": "dBi", "operator": ">=",
         "target": gain_target, "hard": True},
    ]


def _entry(run_id, gain, s11=-15.0, passed_gain=True):
    return {
        "run_id": run_id,
        "feasible": passed_gain,
        "metrics": {
            "gain_h": {"value": gain, "passed": 1 if passed_gain else 0},
            "s11": {"value": s11, "passed": 1},
        },
        "params": {},
    }


class ConvergenceAdapter:
    def __init__(self, assessment="converged", native=None, workdir=None):
        self.assessment = assessment
        self.native = native
        self.workdir = Path(workdir) if workdir else None
        self.convergence_calls = 0

    def preflight(self):
        return {"validation_passed": True}

    def submit(self):
        return {"started": True}

    def wait(self, timeout_s, poll_interval_s):
        return {"running": False, "poll_count": 1}

    def finalize(self):
        return {"saved": True, "solution_data_complete": True}

    def export_convergence(self, path):
        self.convergence_calls += 1
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("convergence", encoding="utf-8")
        return {"path": str(path), "evidence": {"assessment": self.assessment,
                                                "native_converged": self.native}}

    def export_touchstone(self, path):
        _write_s1p(path)
        return {"path": str(path)}

    def export_farfield(self, output_dir, cuts, frequencies=None):
        theta = Path(output_dir) / "theta.csv"
        phi = Path(output_dir) / "phi.csv"
        _write_theta(theta)
        _write_phi(phi)
        return {"cuts": {"theta_phi0": {"path": str(theta)},
                         "phi_theta90": {"path": str(phi)}}}


class ConvergenceGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.root = Path(self.tmp.name)
        self.db = self.root / "campaign.sqlite"

    def tearDown(self):
        for _ in range(10):
            try:
                self.tmp.cleanup()
                break
            except PermissionError:
                time.sleep(0.2)

    def _run(self, adapter, policy=None):
        spec = mcp_server.new_spec("lens", "lens", [24.0, 30.0], _metrics(),
                                   execution_policy=policy)
        store = Store(self.db)
        store.add_run("r1", "lens", 1, {})
        runner = CampaignRunner(store=store, adapter=adapter, spec=spec, run_id="r1",
                                artifact_dir=self.root / "runs" / "r1" / "artifacts",
                                frequency_ghz=28.0)
        status = runner.run()
        store.close()
        return status, spec

    def test_not_converged_fails_closed(self):
        status, _ = self._run(ConvergenceAdapter(assessment="not_converged"))
        self.assertEqual(status, FAILED)

    def test_native_converged_overrides_inconclusive_assessment(self):
        status, _ = self._run(ConvergenceAdapter(assessment="inconclusive", native=True))
        self.assertEqual(status, "COMPLETED")

    def test_converged_completes_and_records_evidence(self):
        status, _ = self._run(ConvergenceAdapter())
        self.assertEqual(status, "COMPLETED")
        with Store(self.db) as store:
            detail = store.get_run("r1")
        log = json.loads(detail["notes"])
        completed = next(e for e in log if e["stage"] == "COMPLETED")
        self.assertEqual(completed["convergence"]["assessment"], "converged")

    def test_policy_can_disable_convergence_gate(self):
        adapter = ConvergenceAdapter(assessment="not_converged")
        status, _ = self._run(adapter, policy={"require_convergence": False})
        self.assertEqual(status, "COMPLETED")
        self.assertEqual(adapter.convergence_calls, 0)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.root = Path(self.tmp.name)
        self.spec = mcp_server.new_spec("lens", "lens", [24.0, 30.0], _metrics())

    def tearDown(self):
        for _ in range(10):
            try:
                self.tmp.cleanup()
                break
            except PermissionError:
                time.sleep(0.2)

    def test_feasibility_and_ranking(self):
        good = _entry("g", 6.0)
        better = _entry("b", 7.5)
        bad = _entry("x", 9.0, passed_gain=False)
        self.assertTrue(archive.is_feasible(good["metrics"], self.spec))
        self.assertFalse(archive.is_feasible(bad["metrics"], self.spec))
        ranked = archive.rank([good, bad, better], archive.default_objectives(self.spec))
        self.assertEqual([e["run_id"] for e in ranked], ["b", "g", "x"])

    def test_pareto_excludes_dominated_and_infeasible(self):
        self.spec["objectives"] = [
            {"metric": "gain_h", "direction": "maximize"},
            {"metric": "s11", "direction": "minimize"},
        ]
        a = _entry("a", 7.0, s11=-20.0)
        b = _entry("b", 6.0, s11=-15.0)  # dominated by a
        c = _entry("c", 8.0, s11=-12.0)  # trade-off, non-dominated
        d = _entry("d", 99.0, passed_gain=False)  # infeasible
        front = archive.pareto_front([a, b, c, d], archive.default_objectives(self.spec))
        self.assertEqual({e["run_id"] for e in front}, {"a", "c"})

    def test_commit_best_refuses_infeasible_overwrite(self):
        feasible = _entry("f", 7.0)
        first = archive.commit_best(self.root, feasible, self.spec)
        self.assertTrue(first["updated"])
        infeasible = _entry("i", 12.0, passed_gain=False)
        refused = archive.commit_best(self.root, infeasible, self.spec)
        self.assertFalse(refused["updated"])
        self.assertEqual(refused["reason"], "refused_infeasible_overwrite")
        self.assertEqual(archive.read_best(self.root)["run_id"], "f")

    def test_commit_best_replaces_when_improved(self):
        archive.commit_best(self.root, _entry("a", 6.0), self.spec)
        better = archive.commit_best(self.root, _entry("b", 7.0), self.spec)
        self.assertTrue(better["updated"])
        self.assertEqual(archive.read_best(self.root)["run_id"], "b")
        worse = archive.commit_best(self.root, _entry("c", 5.0), self.spec)
        self.assertFalse(worse["updated"])
        self.assertEqual(worse["reason"], "not_better_than_best")

    def test_archive_query_tool(self):
        campaign = str(self.root / "camp")
        mcp_server.spec_create(campaign, "lens", "metal_lens", [24.0, 30.0], _metrics())
        with Store(Path(campaign) / "campaign.sqlite") as store:
            store.add_run("r1", "lens", 1, {}, status="COMPLETED")
            store.add_metrics("r1", {"gain_h": {"value": 6.0}, "s11": {"value": -15.0}},
                              {"gain_h": True, "s11": True})
        archive.commit_best(Path(campaign), _entry("r1", 6.0), self.spec)
        result = mcp_server.archive_query(campaign)
        self.assertEqual(result["best"]["run_id"], "r1")
        self.assertEqual(result["objectives"][0]["direction"], "maximize")
        self.assertEqual(len(result["ranked"]), 1)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.campaign = str(Path(self.tmp.name) / "camp")
        mcp_server.spec_create(self.campaign, "lens", "metal_lens", [24.0, 30.0], _metrics())
        self.calls = []

    def tearDown(self):
        for _ in range(10):
            try:
                self.tmp.cleanup()
                break
            except PermissionError:
                time.sleep(0.2)

    def _fake_execute(self, target, command, arguments, timeout=None):
        self.calls.append(command)
        if command == "preflight_design":
            return {"validation_passed": True, "ff_sphere_present": True}
        if command == "analyze_design":
            return {"started": True}
        if command == "analysis_status":
            return {"running": False}
        if command == "finalize_solve":
            return {"saved": True, "solution_data_complete": True}
        if command == "export_results":
            path = Path(arguments["output_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            if arguments.get("export_type") == "convergence":
                path.write_text("convergence", encoding="utf-8")
                return {"path": str(path), "evidence": {"assessment": "converged"}}
            _write_s1p(path)
            return {"path": str(path)}
        if command == "export_farfield_report":
            theta = Path(arguments["output_dir"]) / "t.csv"
            phi = Path(arguments["output_dir"]) / "p.csv"
            _write_theta(theta)
            _write_phi(phi)
            return {"cuts": {"theta_phi0": {"path": str(theta)},
                             "phi_theta90": {"path": str(phi)}}}
        raise AssertionError(f"unexpected command: {command}")

    def _wait_terminal(self, run_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = mcp_server.get_run_status(self.campaign, run_id)
            if status["terminal"]:
                time.sleep(1.0)  # let the background thread close its SQLite handle
                return status
            time.sleep(0.05)
        return status

    def test_identical_submission_hits_cache(self):
        with mock.patch.object(mcp_server.worker_client, "execute", side_effect=self._fake_execute):
            first = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", params={"r": 60},
                pid=1, cuts=["theta_phi0", "phi_theta90"], poll_interval_s=0.01,
                timeout_s=5.0, frequency_ghz=28.0,
            )
            self.assertFalse(first.get("cached"))
            status = self._wait_terminal(first["run_id"])
            self.assertEqual(status["status"], "COMPLETED", status)
            before = len(self.calls)
            second = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", params={"r": 60},
                pid=1, cuts=["theta_phi0", "phi_theta90"], poll_interval_s=0.01,
                timeout_s=5.0, frequency_ghz=28.0,
            )
        self.assertTrue(second["cached"])
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertIn("gain_h", second["metrics"])
        self.assertEqual(len(self.calls), before)  # no new worker calls

    def test_changed_params_miss_cache(self):
        with mock.patch.object(mcp_server.worker_client, "execute", side_effect=self._fake_execute):
            first = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", params={"r": 60},
                pid=1, poll_interval_s=0.01, timeout_s=5.0, frequency_ghz=28.0,
            )
            self._wait_terminal(first["run_id"])
            second = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", params={"r": 70},
                pid=1, poll_interval_s=0.01, timeout_s=5.0, frequency_ghz=28.0,
            )
            self._wait_terminal(second["run_id"])
        self.assertNotEqual(second["run_id"], first["run_id"])
        self.assertFalse(second.get("cached"))

    def test_archive_committed_after_completion(self):
        with mock.patch.object(mcp_server.worker_client, "execute", side_effect=self._fake_execute):
            first = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", params={"r": 60},
                pid=1, poll_interval_s=0.01, timeout_s=5.0, frequency_ghz=28.0,
            )
            self._wait_terminal(first["run_id"])
        best = archive.read_best(self.campaign)
        self.assertIsNotNone(best)
        self.assertEqual(best["run_id"], first["run_id"])


if __name__ == "__main__":
    unittest.main()




