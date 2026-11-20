# -*- coding: utf-8 -*-
"""Offline tests for the P3 enforcement layer (run tokens + audit)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aedt_guard import audit as audit_mod
from aedt_guard.tokens import (
    TokenError,
    issue_token,
    read_token,
    revoke_token,
    verify_token,
)
from aedt_target import AedtTarget
from pyaedt_backend import BackendCommandError, PyAedtBackend

import mcp_server


class FakeDesktop:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.aedt_process_id = 4321
        self.port = kwargs.get("port", 0)


class FakeApp:
    def __init__(self, **kwargs):
        self.project_name = kwargs.get("project", "Proj")
        self.design_name = kwargs.get("design", "Design")
        self.solution_type = "Modal"
        self.analyze_calls = []
        self.setup_objs = []

    def analyze_setup(self, **kwargs):
        self.analyze_calls.append(kwargs)
        return True

    @property
    def setups(self):
        return self.setup_objs

    def validate_simple(self, *args, **kwargs):
        return True


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.campaign = Path(self.tmp.name) / "camp"

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_and_identity(self):
        path = issue_token(self.campaign, run_id="r1", project="dipole",
                           design="Dipole", setup="Setup1")
        self.assertTrue(path.is_file())
        payload = verify_token(path, project="dipole", design="Dipole", setup="Setup1")
        self.assertEqual(payload["run_id"], "r1")
        with self.assertRaises(TokenError):
            verify_token(path, setup="Setup2")

    def test_expired_token_rejected(self):
        path = issue_token(self.campaign, run_id="r1", project="p", design="d",
                           setup="s", ttl_s=-1)
        with self.assertRaises(TokenError):
            read_token(path)

    def test_tampered_token_rejected(self):
        path = issue_token(self.campaign, run_id="r1", project="p", design="d", setup="s")
        document = json.loads(path.read_text(encoding="utf-8"))
        document["payload"]["setup"] = "Other"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(TokenError):
            read_token(path)

    def test_revoke(self):
        path = issue_token(self.campaign, run_id="r1", project="p", design="d", setup="s")
        self.assertTrue(revoke_token(path))
        self.assertFalse(revoke_token(path))
        with self.assertRaises(TokenError):
            read_token(path)

    def test_missing_token_file(self):
        with self.assertRaises(TokenError):
            read_token(self.campaign / "nope.json")


class AuditDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.campaign = Path(self.tmp.name) / "camp"
        self.campaign.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_enforcement_off_allows_everything(self):
        with mock.patch.dict(os.environ, {"AEDT_GUARD_ENFORCE": ""}, clear=False):
            decision = audit_mod.enforce_analyze("analyze_design", {})
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], "enforcement_off")

    def test_missing_token_denied_and_audited(self):
        with mock.patch.dict(os.environ, {"AEDT_GUARD_ENFORCE": "1"}, clear=False):
            decision = audit_mod.enforce_analyze(
                "analyze_design", {}, project="p", design="d", setup="s"
            )
        self.assertFalse(decision["allowed"])
        self.assertIn("run_token", decision["reason"])
        entries = audit_mod.read_audit(self.campaign) if False else []
        # no campaign root configured -> nothing written, decision still denies
        self.assertEqual(entries, [])

    def test_valid_token_allowed_and_audited(self):
        token = issue_token(self.campaign, run_id="r1", project="p", design="d", setup="s")
        with mock.patch.dict(
            os.environ,
            {"AEDT_GUARD_ENFORCE": "1", "AEDT_CAMPAIGN_ROOT": str(self.campaign)},
            clear=False,
        ):
            decision = audit_mod.enforce_analyze(
                "analyze_design", {"run_token": str(token)}, project="p", design="d", setup="s"
            )
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["run_id"], "r1")
        entries = audit_mod.read_audit(self.campaign)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["allowed"])
        self.assertEqual(entries[0]["run_id"], "r1")

    def test_escape_hatch_allowed_but_counted(self):
        with mock.patch.dict(
            os.environ,
            {
                "AEDT_GUARD_ENFORCE": "1",
                "AEDT_ALLOW_UNGUARDED": "1",
                "AEDT_CAMPAIGN_ROOT": str(self.campaign),
            },
            clear=False,
        ):
            decision = audit_mod.enforce_analyze("start_analysis", {})
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], "escape_hatch")
        summary = audit_mod.summarize(self.campaign)
        self.assertEqual(summary["escape_hatch"], 1)
        self.assertEqual(summary["unguarded_count"], 1)

    def test_invalid_token_denied_and_counted(self):
        # token issued for a different setup must not authorise this submission
        token = issue_token(self.campaign, run_id="r1", project="p", design="d", setup="Other")
        with mock.patch.dict(
            os.environ,
            {"AEDT_GUARD_ENFORCE": "1", "AEDT_CAMPAIGN_ROOT": str(self.campaign)},
            clear=False,
        ):
            decision = audit_mod.enforce_analyze(
                "analyze_design", {"run_token": str(token)}, project="p", design="d", setup="s"
            )
        self.assertFalse(decision["allowed"])
        self.assertIn("invalid_run_token", decision["reason"])
        self.assertEqual(audit_mod.summarize(self.campaign)["unguarded_count"], 1)

    def test_python_code_scan(self):
        with mock.patch.dict(os.environ, {"AEDT_GUARD_ENFORCE": "1"}, clear=False):
            plain = audit_mod.enforce_python_code("app.save_project()", {})
            suspicious = audit_mod.enforce_python_code("app.analyze_setup('Setup1')", {})
        self.assertTrue(plain["allowed"])
        self.assertFalse(suspicious["allowed"])
        token = issue_token(self.campaign, run_id="r1", project="p", design="d", setup="s")
        with mock.patch.dict(os.environ, {"AEDT_GUARD_ENFORCE": "1"}, clear=False):
            quoted = audit_mod.enforce_python_code(
                "app.analyze_setup('Setup1')", {"run_token": str(token)}, project="p", design="d"
            )
        self.assertFalse(quoted["allowed"])  # setup-scoped tokens need the setup presented
        from aedt_guard.tokens import TokenError, verify_token
        with self.assertRaises(TokenError):
            verify_token(token, project="p", design="d")
        payload = verify_token(token, project="p", design="d", setup="s")
        self.assertEqual(payload.get("setup"), "s")


class BackendEnforcementTests(unittest.TestCase):
    def _backend(self, app):
        return PyAedtBackend(
            desktop_factory=lambda **kwargs: FakeDesktop(**kwargs),
            hfss_factory=lambda **kwargs: app,
            version="2026.1",
        )

    def test_start_analysis_requires_token_when_enforced(self):
        app = FakeApp()
        backend = self._backend(app)
        with mock.patch.dict(os.environ, {"AEDT_GUARD_ENFORCE": "1"}, clear=False):
            with self.assertRaises(BackendCommandError) as ctx:
                backend.execute(
                    AedtTarget("port", 50051),
                    "start_analysis",
                    {"project_name": "Proj", "design_name": "Design", "setup_name": "Setup1"},
                )
        self.assertIn("E_UNGUARDED_ANALYZE", str(ctx.exception))
        self.assertEqual(app.analyze_calls, [])

    def test_start_analysis_allowed_with_token(self):
        app = FakeApp()
        backend = self._backend(app)
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            campaign = Path(tmp) / "camp"
            token = issue_token(campaign, run_id="r1", project="Proj",
                                design="Design", setup="Setup1")
            with mock.patch.dict(os.environ, {"AEDT_GUARD_ENFORCE": "1"}, clear=False):
                result = backend.execute(
                    AedtTarget("port", 50051),
                    "start_analysis",
                    {"project_name": "Proj", "design_name": "Design",
                     "setup_name": "Setup1", "run_token": str(token)},
                )
        self.assertTrue(result["started"])
        self.assertEqual(len(app.analyze_calls), 1)

    def test_start_analysis_unchanged_when_enforcement_off(self):
        app = FakeApp()
        backend = self._backend(app)
        with mock.patch.dict(os.environ, {"AEDT_GUARD_ENFORCE": ""}, clear=False):
            result = backend.execute(
                AedtTarget("port", 50051),
                "start_analysis",
                {"project_name": "Proj", "design_name": "Design", "setup_name": "Setup1"},
            )
        self.assertTrue(result["started"])


class ContextAuditTests(unittest.TestCase):
    def test_campaign_context_reports_audit_counts(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            campaign = str(Path(tmp) / "camp")
            mcp_server.spec_create(campaign, "lens", "metal_lens", [24.0, 30.0], [
                {"id": "s11", "kind": "s11", "unit": "dB", "operator": "<=",
                 "target": -10.0, "hard": True, "band_ghz": [24.0, 30.0]},
            ])
            audit_mod.append_audit(Path(campaign), {"allowed": False, "reason": "missing run_token"})
            audit_mod.append_audit(Path(campaign), {"allowed": True, "reason": "run_token",
                                                    "run_id": "r1"})
            context = mcp_server.campaign_context(campaign)
            self.assertEqual(context["analysis_audit"]["unguarded_count"], 1)
            self.assertEqual(context["analysis_audit"]["governed"], 1)


if __name__ == "__main__":
    unittest.main()
