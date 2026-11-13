"""The directivity column is opt-in: default tasks must not request it."""
from __future__ import annotations

import unittest
from pathlib import Path
import tempfile


class FarfieldFlagTests(unittest.TestCase):
    def test_default_spec_does_not_ask_for_directivity(self):
        from aedt_guard.spec import DEFAULT_EVALUATION

        self.assertNotIn("include_directivity", DEFAULT_EVALUATION)

    def test_worker_adapter_only_forwards_when_requested(self):
        from aedt_guard.worker_adapter import GuardedAdapter

        calls = []

        class Recorder:
            def _call(self, command, arguments):
                calls.append((command, dict(arguments)))
                return {"ok": True}

            def __getattr__(self, name):
                return self._call

        adapter = object.__new__(GuardedAdapter)
        adapter.project_name = "p"
        adapter.design_name = "d"
        adapter.setup_name = "P4"
        adapter.farfield_setup = "P4"
        adapter.sweep_name = "ANT_Band"
        adapter._call = Recorder()._call

        with tempfile.TemporaryDirectory() as tmp:
            adapter.export_farfield(Path(tmp), ["theta_phi0"], frequencies=[28.0])
            adapter.export_farfield(Path(tmp), ["theta_phi0"], frequencies=[28.0],
                                    include_directivity=True)
        self.assertNotIn("include_directivity", calls[0][1])
        self.assertTrue(calls[1][1]["include_directivity"])


if __name__ == "__main__":
    unittest.main()


class SphereGridRequestTests(unittest.TestCase):
    def test_grid_cut_is_added_once_when_directivity_is_requested(self):
        import inspect

        from aedt_guard import sim

        source = inspect.getsource(sim)
        self.assertIn('export_cuts = export_cuts + ["sphere_grid"]', source)
        # the cuts must be passed once: passing them positionally and as a keyword raised
        # "got multiple values for argument 'cuts'" on the first real run
        self.assertIn('self.artifact_dir / "farfield", export_cuts', source)
        self.assertNotIn('farfield_extra["cuts"]', source)
