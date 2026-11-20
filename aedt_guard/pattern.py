"""Read a full-sphere gain grid and turn it into directivity numbers.

Two consumers: the guard evaluator (peak directivity, radiation efficiency) and the design brief that
asks for radiation concentrated at theta = 90 deg (the same grid's horizontal slice). Both come from
one integral:

    eta          = integral(G dOmega) / 4pi
    D(theta,phi) = G(theta,phi) / eta

Integrating the gain pattern is legitimate for a passive antenna, which this lens is.
"""
from __future__ import annotations

import math
from pathlib import Path

__all__ = ["read_sphere_grid", "directivity_from_grid", "directivity_horizontal_from_grid", "CUT"]

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
    header = [cell.strip().strip('"') for cell in rows[0]]

    def column(prefix: str) -> int | None:
        for index, name in enumerate(header):
            if name.lower().startswith(prefix.lower()):
                return index
        return None

    theta_column = column("Theta")
    phi_column = column("Phi")
    value_columns = [index for index, name in enumerate(header) if name.startswith("dB(")]
    if not value_columns:
        raise ValueError("sphere grid has no dB(...) column: %s" % header[:6])

    if theta_column is not None and phi_column is not None and len(value_columns) == 1:
        # flattened layout: one row per (theta, phi); pivot into a theta-major grid
        value_column = value_columns[0]
        cells: dict[tuple[float, float], float] = {}
        for row in rows[1:]:
            if len(row) <= max(theta_column, phi_column, value_column):
                continue
            try:
                theta = float(row[theta_column])
                phi = float(row[phi_column])
                value = float(row[value_column])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            cells[(round(theta, 6), round(phi, 6))] = value
        if len(cells) < 3:
            raise ValueError("sphere grid has too few finite samples: %s" % target)
        thetas = sorted({theta for theta, _phi in cells})
        phis = sorted({phi for _theta, phi in cells})
        values = [[cells.get((theta, phi), MIN_DB) for phi in phis] for theta in thetas]
        if len(thetas) < 3 or len(phis) < 3:
            raise ValueError("sphere grid is not two-dimensional: %d x %d"
                             % (len(thetas), len(phis)))
        return thetas, phis, values

    # matrix layout: one row per theta, one dB column per phi family
    if theta_column is None:
        raise ValueError("sphere grid has no Theta column: %s" % header[:6])
    grid: dict[float, dict[int, float]] = {}
    for row in rows[1:]:
        if len(row) <= max(value_columns):
            continue
        try:
            theta = float(row[theta_column])
        except (TypeError, ValueError):
            continue
        slot = grid.setdefault(theta, {})
        for index in value_columns:
            try:
                slot[index] = float(row[index])
            except (TypeError, ValueError):
                continue
    thetas = sorted(grid)
    ordered_phis = [float(index) for index in value_columns]
    values = [[grid[theta].get(index, MIN_DB) for index in value_columns] for theta in thetas]
    return thetas, ordered_phis, values


def _integrate(thetas: list[float], values: list[list[float]]) -> tuple[float, float, float]:
    """Return (total linear gain integral, peak linear gain, eta) for a full-sphere grid."""
    if len(thetas) < 3 or not values or len(values[0]) < 3:
        raise ValueError("a directivity integral needs at least 3x3 samples")
    if max(thetas) - min(thetas) < 170.0:
        raise ValueError("theta must cover (almost) the full 0..180 degrees")
    columns = len(values[0])
    d_theta = math.radians((max(thetas) - min(thetas)) / (len(thetas) - 1))
    # phi is periodic and the export repeats the first column at 360: skip the duplicate
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
    return total, peak, total / (4.0 * math.pi)


def directivity_from_grid(thetas: list[float], values: list[list[float]]) -> float:
    """Peak directivity (dB) of a theta/phi grid that spans the full sphere in both directions."""
    total, peak, _eta = _integrate(thetas, values)
    return 10.0 * math.log10(4.0 * math.pi * peak / total)


def directivity_horizontal_from_grid(thetas: list[float],
                                     values: list[list[float]],
                                     theta_deg: float = 90.0) -> dict[str, float]:
    """Peak directivity in the horizontal plane, plus the efficiency hidden in the integral.

    Returns ``directivity_db`` (global peak), ``horizontal_db`` (peak at the requested theta),
    ``horizontal_theta`` (the sampled theta actually used), ``horizontal_phi`` (its phi index) and
    ``radiation_efficiency``. A gain grid cannot separate loss from directivity without the integral,
    so the efficiency here is the same number the peak path would produce - reported once.
    """
    total, peak, eta = _integrate(thetas, values)
    row = min(range(len(thetas)), key=lambda index: abs(thetas[index] - theta_deg))
    if abs(thetas[row] - theta_deg) > 2.0:
        raise ValueError("theta grid has no sample within 2 degrees of %.1f" % theta_deg)
    columns = len(values[0])
    counted = columns - 1 if columns > 1 else columns
    strongest = max(range(counted), key=lambda column: values[row][column])
    value = values[row][strongest]
    horizontal = 0.0 if value < MIN_DB else 10.0 ** (value / 10.0)
    if horizontal <= 0.0:
        raise ValueError("no radiated power in the horizontal plane")
    return {
        "directivity_db": 10.0 * math.log10(4.0 * math.pi * peak / total),
        "horizontal_db": 10.0 * math.log10(4.0 * math.pi * horizontal / total),
        "horizontal_theta": float(thetas[row]),
        "horizontal_phi": float(strongest * (360.0 / counted)) if counted else 0.0,
        "radiation_efficiency": float(min(max(eta, 0.0), 1.0)),
    }
