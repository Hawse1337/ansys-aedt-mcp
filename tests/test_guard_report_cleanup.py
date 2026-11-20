"""Guard-created reports are deleted after export, so later jobs do not re-trigger AEDT plot probing."""
from __future__ import annotations

import unittest
from unittest import mock

from pyaedt_backend import _drop_report


class FakePost:
    def __init__(self, fail: bool = False) -> None:
        self.deleted: list[str] = []
        self.fail = fail

    def delete_report(self, name: str) -> bool:
        self.deleted.append(name)
        if self.fail:
            raise RuntimeError("report not found")
        return True


class DropReportTests(unittest.TestCase):
    def test_a_named_report_is_deleted(self):
        post = FakePost()
        _drop_report(post, "Plot_ABC123")
        self.assertEqual(post.deleted, ["Plot_ABC123"])

    def test_a_failing_delete_does_not_raise(self):
        post = FakePost(fail=True)
        _drop_report(post, "Plot_ABC123")
        self.assertEqual(post.deleted, ["Plot_ABC123"])

    def test_a_post_without_delete_report_is_tolerated(self):
        class Old:
            pass

        _drop_report(Old(), "Plot_ABC123")

    def test_the_farfield_export_path_calls_it(self):
        """The two export sites must drop the report they created."""
        import inspect

        import pyaedt_backend

        source = inspect.getsource(pyaedt_backend.PyAedtBackend._export_farfield_report)
        self.assertEqual(source.count("_drop_report(post, report_name)"), 1)
        self.assertEqual(source.count("_drop_report(post, direct_name)"), 1)


if __name__ == "__main__":
    unittest.main()
