# -*- coding: utf-8 -*-
"""Tests for the fixed read-only AEDT report-image exporter (no AEDT)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aedt_target import AedtTarget
from pyaedt_backend import PyAedtBackend


class _FakePost:
    def __init__(self, outdir: Path):
        self.outdir = outdir
        self.created = []
        self.deleted = []

    def create_report(self, **kwargs):
        self.created.append(kwargs)
        return kwargs["plot_name"]

    def export_report_to_jpg(self, directory, report_name, **kwargs):
        path = Path(directory) / (report_name + ".jpg")
        path.write_bytes(b"jpeg")
        return str(path)

    def export_report_to_csv(self, *_args, **_kwargs):
        raise AssertionError("image exporter must not request CSV")

    def delete_report(self, name):
        self.deleted.append(name)
        return True


def test_export_report_image_creates_three_native_reports(tmp_path: Path):
    post = _FakePost(tmp_path)
    app = SimpleNamespace(post=post, project_name="p", design_name="d")
    backend = object.__new__(PyAedtBackend)
    result = backend._export_report_image(
        app,
        AedtTarget("port", 12345),
        {
            "setup_name": "Setup1",
            "output_dir": str(tmp_path),
            "frequency": "12GHz",
            "plots": ["3d", "e", "h"],
            "width": 800,
            "height": 600,
        },
    )
    assert result["read_only"] is True
    assert result["frequency"] == "12GHz"
    assert sorted(result["files"]) == ["P3D_12", "PE_12", "PH_12"]
    assert all(row["exists"] for row in result["files"].values())
    assert result["errors"] == []
    assert [call["plot_type"] for call in post.created] == [
        "3D Polar Plot", "Polar Plot", "Polar Plot"
    ]
    assert sorted(post.deleted) == ["P3D_12", "PE_12", "PH_12"]


def test_export_report_image_rejects_unknown_plot(tmp_path: Path):
    post = _FakePost(tmp_path)
    app = SimpleNamespace(post=post, project_name="p", design_name="d")
    backend = object.__new__(PyAedtBackend)
    try:
        backend._export_report_image(
            app, AedtTarget("port", 12345),
            {"output_dir": str(tmp_path), "plots": ["magic"]},
        )
    except Exception as exc:  # noqa: BLE001
        assert "unknown plots" in str(exc)
    else:
        raise AssertionError("unknown plot type must be rejected")
