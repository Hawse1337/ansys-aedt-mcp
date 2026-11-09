"""Guarded-workflow layer for AEDT antenna campaigns.

This package is pure Python and never talks to AEDT: it owns the design
specification, the run/metrics store and the artifact-based evaluator. The MCP
tools in ``mcp_server`` wire it to the PyAEDT worker.
"""
from __future__ import annotations

from .evaluator import (
    ArtifactError,
    evaluate,
    horizontal_level,
    passivity,
    peak_and_beam,
    read_curve_csv,
    read_touchstone,
    realized_gain,
    ripple,
    s11_band,
    sidelobe_level,
)
from .spec import (
    SCHEMA_VERSION,
    SpecError,
    hard_metrics,
    load_spec,
    new_spec,
    resolve_frequencies,
    resolve_solver_settings,
    save_spec,
    soft_metrics,
    task_context,
    update_spec,
    validate_spec,
)
from .store import Store

__all__ = [
    "SCHEMA_VERSION",
    "SpecError",
    "ArtifactError",
    "Store",
    "evaluate",
    "hard_metrics",
    "horizontal_level",
    "load_spec",
    "new_spec",
    "resolve_frequencies",
    "resolve_solver_settings",
    "passivity",
    "peak_and_beam",
    "read_curve_csv",
    "read_touchstone",
    "realized_gain",
    "ripple",
    "s11_band",
    "save_spec",
    "sidelobe_level",
    "soft_metrics",
    "task_context",
    "update_spec",
    "validate_spec",
]


