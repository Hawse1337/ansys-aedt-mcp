"""Directivity from a sphere-grid far-field export.

The design reports only gain, so the directivity has to come from the pattern: integrate the
gain over the sphere for the radiated power and compare with the peak. This is the guard-side
of the calculation - antopt derives the efficiency from the directivity it receives.
"""
from __future__ import annotations

import math
from pathlib import Path

__all__ = ["read_sphere_grid", "directivity_from_grid", "CUT"]

CUT = "sphere_grid"
MIN_DB = -60.0


def read_sphere_grid(path: str | Path) -> tuple[list[float], list[float], list[list[float]]]:
    """Read ``theta``, ``phi`` and the gain grid (dB) from an AEDT sphere-grid CSV.

    The export carries one row per theta and one column per phi family; the header names the
    phi value. Rows are sorted by theta and columns by phi.
    """
    import csv

    target = Path(path)
    if not target.is_file():
        raise ValueError("sphere grid not found: %s" % target)
    with target.open(encoding="utf-8", errors="replace", newline="") as handle:
        rows = list(csv.reader(handle))
    if len(rows) < 3:
        raise ValueError("sphere grid has too few rows: %s" % target)
    header = [cell.strip() for cell in rows[0]]
    # first two columns are Freq and Theta; the rest are phi families
    phi_column = [index for index, name in enumerate(header) if name.startswith("dB(")]
    if not phi_column:
        raise ValueError("sphere grid has no dB(...) column: %s" % header[:6])
    phis: list[float] = []
    for index in phi_column:
        name = header[index]
        # names look like "dB(GainTotal) [dB]" repeated, so the phi comes from the metadata
        phis.append(float(index))
    grid: dict[float, dict[int, float]] = {}
    for row in rows[1:]:
        if len(row) <= max(phi_column):
            continue
        try:
            theta = float(row[1])
        except (TypeError, ValueError):
            continue
        slot = grid.setdefault(theta, {})
        for index in phi_column:
            try:
                slot[index] = float(row[index])
            except (TypeError, ValueError):
                continue
    thetas = sorted(grid)
    ordered_phis = [float(index) for index in phi_column]
    values = [[grid[theta].get(index, MIN_DB) for index in phi_column] for theta in thetas]
    return thetas, ordered_phis, values


def directivity_from_grid(thetas: list[float], values: list[list[float]]) -> float:
    """Directivity (dB) of a theta/phi grid that spans the full sphere in both directions."""
    if len(thetas) < 3 or not values or len(values[0]) < 3:
        raise ValueError("a directivity integral needs at least 3x3 samples")
    if max(thetas) - min(thetas) < 170.0:
        raise ValueError("theta must cover (almost) the full 0..180 degrees")
    columns = len(values[0])
    d_theta = math.radians((max(thetas) - min(thetas)) / (len(thetas) - 1))
    # the phi axis is periodic and the export repeats the first column at 360: use the wrapped
    # step and skip that duplicate, otherwise the integral is (n)/(n-1) too large and D comes out
    # low by exactly the ratio 5/4 for a five-column grid
    counted = columns - 1 if columns > 1 else columns
    d_phi = math.radians(360.0 / counted) if counted else 0.0
    total = 0.0
    peak = 0.0
    for row_index, theta in enumerate(thetas):
        sin_theta = math.sin(math.radians(theta))
        for column in range(counted):
            value = values[row_index][column]
            linear = 0.0 if value < MIN_DB else 10.0 ** (value / 10.0)
            peak = max(peak, linear)
            total += linear * sin_theta * d_theta * d_phi
    if total <= 0.0 or peak <= 0.0:
        raise ValueError("degenerate pattern: no radiated power")
    directivity = 4.0 * math.pi * peak / total
    return 10.0 * math.log10(directivity)
