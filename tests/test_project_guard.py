# -*- coding: utf-8 -*-
"""Tests for aedt_project_guard (no AEDT session required)."""
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import aedt_project_guard as guard


class ResolveProjectArgumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.existing = self.dir / "LensFinal.aedt"
        self.existing.write_text("$begin 'AnsoftProject'\n", encoding="utf-8")
        self.dirs = [str(self.dir)]
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_none_and_empty_are_untouched(self):
        self.assertIsNone(guard.resolve_project_argument(None, self.dirs))
        self.assertEqual(guard.resolve_project_argument("", self.dirs), "")

    def test_bare_name_matching_a_file_becomes_absolute_path(self):
        resolved = guard.resolve_project_argument("LensFinal", self.dirs)
        self.assertEqual(resolved, str(self.existing))
        self.assertTrue(os.path.isabs(resolved))

    def test_bare_name_with_extension_is_also_resolved(self):
        resolved = guard.resolve_project_argument("LensFinal.aedt", self.dirs)
        self.assertEqual(resolved, str(self.existing))

    def test_absolute_path_is_left_alone(self):
        value = str(self.existing)
        self.assertEqual(guard.resolve_project_argument(value, self.dirs), value)

    def test_absolute_path_that_does_not_exist_is_left_alone(self):
        # explicit location for a NEW project must still work
        value = str(self.dir / "BrandNew.aedt")
        self.assertEqual(guard.resolve_project_argument(value, self.dirs), value)

    def test_path_with_separator_is_left_alone(self):
        value = "sub/Thing.aedt"
        self.assertEqual(guard.resolve_project_argument(value, self.dirs), value)

    def test_unknown_bare_name_is_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            guard.resolve_project_argument("NoSuchProject", self.dirs)
        self.assertIn("refused", str(ctx.exception))
        self.assertIn("OVERWRITE", str(ctx.exception))

    def test_escape_hatch_creates_inside_the_first_search_dir(self):
        # Creation is allowed, but the location must be deterministic: a bare name
        # becomes <first search dir>/<name>.aedt, never PyAEDT's default directory.
        with mock.patch.dict(os.environ, {"AEDT_GUARD_ALLOW_CREATE": "1"}):
            resolved = guard.resolve_project_argument("NoSuchProject", self.dirs)
        self.assertEqual(resolved, os.path.join(str(self.dir), "NoSuchProject.aedt"))
        self.assertTrue(os.path.isabs(resolved))

    def test_attach_open_flag_cannot_bypass_unverified_name(self):
        with mock.patch.dict(os.environ, {"AEDT_GUARD_ATTACH_OPEN": "1", "AEDT_GUARD_ALLOW_CREATE": "1"}):
            with self.assertRaisesRegex(RuntimeError, "unverified"):
                guard.resolve_project_argument("NoSuchProject", self.dirs)

    def test_search_dirs_can_come_from_environment(self):
        other = self.dir / "other"
        other.mkdir()
        target = other / "EnvProj.aedt"
        target.write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AEDT_GUARD_SEARCH_DIRS": str(other)}):
            resolved = guard.resolve_project_argument("EnvProj", [str(self.dir)])
        self.assertEqual(resolved, str(target))


class CreateProjectTests(unittest.TestCase):
    """create_project() is the sanctioned way to create a new project."""

    class FakeApp:
        def __init__(self, project=None, **kwargs):
            self.project = project
            self.kwargs = kwargs

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def test_creates_at_the_requested_absolute_path(self):
        target = self.dir / "BrandNew.aedt"
        app = guard.create_project(self.FakeApp, str(target), design="D1")
        self.assertEqual(app.project, str(target))
        self.assertFalse(target.exists())  # FakeApp must not create the file
        self.assertEqual(app.kwargs["design"], "D1")

    def test_appends_aedt_extension(self):
        app = guard.create_project(self.FakeApp, str(self.dir / "NoExt"))
        self.assertTrue(app.project.endswith("NoExt.aedt"))

    def test_refuses_when_the_file_already_exists(self):
        existing = self.dir / "LensFinal.aedt"
        existing.write_text("x", encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            guard.create_project(self.FakeApp, str(existing))
        self.assertIn("already exists", str(ctx.exception))
        # the file must be untouched
        self.assertEqual(existing.read_text(encoding="utf-8"), "x")

    def test_rejects_a_bare_name(self):
        # A bare name would let PyAEDT pick the directory; refuse it.
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            with self.assertRaises((ValueError, RuntimeError)):
                guard.create_project(self.FakeApp, "BareName.aedt")
        finally:
            os.chdir(old)

    def test_creates_missing_parent_directories(self):
        target = self.dir / "sub" / "deeper" / "New.aedt"
        app = guard.create_project(self.FakeApp, str(target))
        self.assertEqual(app.project, str(target))
        self.assertTrue((self.dir / "sub" / "deeper").is_dir())

    def test_project_kwarg_is_not_duplicated(self):
        target = self.dir / "Dup.aedt"
        app = guard.create_project(self.FakeApp, str(target), project="ignored")
        self.assertEqual(app.project, str(target))


class WrapperTests(unittest.TestCase):
    def test_wrapper_preserves_type_and_rewrites_project(self):
        class Fake:
            def __init__(self, project=None, design=None):
                self.project = project
                self.design = design

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        existing = directory / "Proj.aedt"
        existing.write_text("x", encoding="utf-8")

        guarded = guard._wrap(Fake, "Fake")
        with mock.patch.dict(os.environ, {"AEDT_GUARD_SEARCH_DIRS": str(directory)}):
            instance = guarded(project="Proj", design="D1")

        self.assertIsInstance(instance, Fake)
        self.assertEqual(instance.project, str(existing))
        self.assertEqual(instance.design, "D1")
        self.assertEqual(guarded.__name__, "Fake")

    def test_wrapper_handles_positional_project(self):
        class Fake:
            def __init__(self, project=None):
                self.project = project

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        existing = directory / "Proj.aedt"
        existing.write_text("x", encoding="utf-8")

        guarded = guard._wrap(Fake, "Fake")
        with mock.patch.dict(os.environ, {"AEDT_GUARD_SEARCH_DIRS": str(directory)}):
            instance = guarded("Proj")

        self.assertEqual(instance.project, str(existing))


if __name__ == "__main__":
    unittest.main()
