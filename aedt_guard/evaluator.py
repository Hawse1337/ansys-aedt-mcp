"""Metric extraction and constraint checking for exported AEDT artifacts.

Everything here works on files already written to disk (Touchstone, far-field
CSV) so that evaluation never depends on the state of an AEDT session and can
be re-run later without re-solving.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np


class ArtifactError(ValueError):
    """Raised when an artifact cannot be read as expected."""


def read_touchstone(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (freq_ghz, s11_db) from a one- or two-port Touchstone file.

    The ``#`` option line is honoured (Hz/kHz/MHz/GHz and MA/RI/DB data
    formats), because AEDT exports differ by project settings.
    """
    target = Path(path)
    if not target.is_file():
        raise ArtifactError(f"touchstone not found: {target}")
    unit_scale = 1e-9  # default GHz
    data_format = "MA"
    freqs: list[float] = []
    s11: list[float] = []
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("!"):
            continue
        if text.startswith("#"):
            tokens = [token.upper() for token in text[1:].split()]
            for token, scale in (("HZ", 1e-9), ("KHZ", 1e-6), ("MHZ", 1e-3), ("GHZ", 1.0)):
                if token in tokens:
                    unit_scale = scale
                    break
            for token in ("MA", "RI", "DB"):
                if token in tokens:
                    data_format = token
                    break
            continue
        parts = text.split()
        try:
            freq = float(parts[0])
            first = float(parts[1])
            second = float(parts[2])
        except (IndexError, ValueError):
            continue
        if data_format == "RI":
            magnitude = abs(complex(first, second))
        elif data_format == "DB":
            magnitude = 10 ** (first / 20.0)
        else:
            magnitude = abs(first * np.exp(1j * math.radians(second)))
        freqs.append(freq * unit_scale)
        s11.append(float(20.0 * np.log10(max(magnitude, 1e-12))))
    if not freqs:
        raise ArtifactError(f"no S-parameter rows in {target}")
    order = np.argsort(freqs)
    return np.asarray(freqs)[order], np.asarray(s11)[order]


def read_curve_csv(
    path: str | Path,
    *,
    x_candidates: Iterable[str] = ("Theta [deg]", "Phi [deg]"),
    y_candidates: Iterable[str] = ("dB(GainTotal) []", "dB(RealizedGainTotal) []"),
) -> tuple[np.ndarray, np.ndarray]:
    """Read an AEDT far-field export CSV (header block plus columns)."""
    target = Path(path)
    if not target.is_file():
        raise ArtifactError(f"curve CSV not found: {target}")
    with target.open(encoding="utf-8", errors="replace", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ArtifactError(f"empty curve CSV: {target}")
    header = {key.strip(): key for key in rows[0]}
    x_key = next((header[c] for c in x_candidates if c in header), None)
    y_key = next((header[c] for c in y_candidates if c in header), None)
    if x_key is None or y_key is None:
        raise ArtifactError(f"unrecognised column layout in {target}: {sorted(header)}")
    xs: list[float] = []
    ys: list[float] = []
    for row in rows:
        try:
            xs.append(float(row[x_key]))
            ys.append(float(row[y_key]))
        except (TypeError, ValueError):
            continue
    if not xs:
        raise ArtifactError(f"no numeric rows in {target}")
    return np.asarray(xs), np.asarray(ys)


def s11_band(freqs: np.ndarray, s11_db: np.ndarray, band: tuple[float, float],
             limit_db: float = -10.0) -> dict[str, Any]:
    """Worst value and contiguous in-band span for S11 (GHz)."""
    low, high = band
    mask = (freqs >= low - 1e-9) & (freqs <= high + 1e-9)
    freq = freqs[mask]
    values = s11_db[mask]
    if freq.size == 0:
        raise ArtifactError(f"no samples inside {low}-{high} GHz")
    worst_index = int(np.argmax(values))
    ok = values <= limit_db
    span_low = span_high = None
    best_span = 0.0
    start = None
    for index, good in enumerate(ok):
        if good and start is None:
            start = index
        if (not good or index == len(ok) - 1) and start is not None:
            end = index if not good else index + 1
            if end - start >= 2:
                span = float(freq[end - 1] - freq[start])
                if span > best_span:
                    best_span = span
                    span_low, span_high = float(freq[start]), float(freq[end - 1])
            start = None
    return {
        "worst_db": float(values[worst_index]),
        "worst_freq_ghz": float(freq[worst_index]),
        "samples": int(freq.size),
        "band_ok_fraction": float(ok.mean()),
        "span_ghz": best_span,
        "span_range_ghz": [span_low, span_high],
    }


def realized_gain(gain_db: float | np.ndarray, s11_db: float | np.ndarray):
    """Realized gain from gain and S11: G + 10log10(1-|S11|^2)."""
    mismatch = 1.0 - np.power(10.0, np.asarray(s11_db, dtype=float) / 10.0)
    mismatch = np.maximum(mismatch, 1e-9)
    return np.asarray(gain_db, dtype=float) + 10.0 * np.log10(mismatch)


def peak_and_beam(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    index = int(np.argmax(y))
    return float(y[index]), float(x[index])


def horizontal_level(x: np.ndarray, y: np.ndarray, theta_deg: float = 90.0) -> float:
    index = int(np.argmin(np.abs(x - theta_deg)))
    return float(y[index])


def ripple(x: np.ndarray, y: np.ndarray) -> float:
    return float(np.max(y) - np.min(y))


def sidelobe_level(x: np.ndarray, y: np.ndarray, exclude_main_deg: float = 10.0) -> float:
    """Peak of the theta cut outside the main lobe, in dBc."""
    peak, peak_x = peak_and_beam(x, y)
    outside = np.abs(x - peak_x) > float(exclude_main_deg)
    if not np.any(outside):
        return float("nan")
    return float(np.max(y[outside]) - peak)


def passivity(path: str | Path, tolerance: float = 1e-6) -> dict[str, Any]:
    """Passivity snapshot for a one- or two-port Touchstone export.

    For a lossless two-port network |S11|^2 + |S21|^2 must not exceed one;
    values above ``1 + tolerance`` indicate an active/non-passive result and a
    modelling or export error rather than a physical improvement.
    """
    target = Path(path)
    if not target.is_file():
        raise ArtifactError(f"touchstone not found: {target}")
    rows: list[tuple[float, float, float, float]] = []
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith(("!", "#")):
            continue
        parts = text.split()
        if len(parts) < 5:
            continue
        try:
            values = [float(part) for part in parts[:5]]
        except ValueError:
            continue
        freq, s11_mag, _s11_phase, s21_mag, _s21_phase = values
        rows.append((freq, s11_mag, s21_mag, s11_mag ** 2 + s21_mag ** 2))
    if not rows:
        return {"checked": False, "reason": "need 2-port amplitude data", "violations": 0}
    violations = [r for r in rows if r[3] > 1.0 + tolerance]
    return {
        "checked": True,
        "samples": len(rows),
        "violations": len(violations),
        "worst_power_sum": max(r[3] for r in rows),
        "worst_freq": violations[0][0] if violations else None,
        "passive": not violations,
    }


def _compare(value: float, operator: str, target: float) -> bool:
    if operator == "<=":
        return value <= target
    if operator == ">=":
        return value >= target
    return abs(value - target) < 1e-9


def evaluate_frequencies(artifacts: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a candidate whose far fields were exported at several frequencies.

    ``artifacts["farfield"]`` maps frequency (GHz) to ``{"theta": path,
    "phi": path}``. Per-frequency values are kept in the detail block; the
    scalar used for hard constraints is the worst case across frequencies
    (minimum gain, maximum ripple, maximum sidelobe, maximum beam drift).
    """
    evaluation = spec.get("evaluation", {})
    band = tuple(spec["frequency_ghz"][:2])
    exclusion = float(evaluation.get("main_lobe_exclusion_deg", 10))
    horiz_theta = float(evaluation.get("horiz_theta_deg", 90))
    mode = evaluation.get("realized_gain_mode", "gain_plus_mismatch")

    result: dict[str, Any] = {"metrics": {}, "hard": {}, "unmet": [], "invalid": None}
    farfield = artifacts.get("farfield") or {}
    if not farfield:
        result["invalid"] = "no farfield artifacts"
        result["feasible"] = False
        return result
    try:
        freqs, s11_db = read_touchstone(artifacts["s11"])
        s11_info = s11_band(freqs, s11_db, band)
        per_frequency: dict[str, Any] = {}
        for label, paths in sorted(farfield.items(), key=lambda item: float(item[0])):
            gain_freq = float(label)
            theta, gain = read_curve_csv(paths["theta"])
            peak, peak_theta = peak_and_beam(theta, gain)
            horizontal = horizontal_level(theta, gain, theta_deg=horiz_theta)
            s11_at_f = float(np.interp(gain_freq, freqs, s11_db))
            realized = (
                float(realized_gain(horizontal, s11_at_f))
                if mode == "gain_plus_mismatch"
                else horizontal
            )
            phi_gain = None
            ripple_value = None
            if paths.get("phi"):
                phi_x, phi_y = read_curve_csv(paths["phi"], x_candidates=("Phi [deg]", "Theta [deg]"))
                ripple_value = ripple(phi_x, phi_y)
            per_frequency[f"{gain_freq:g}"] = {
                "peak_gain": peak,
                "peak_theta": peak_theta,
                "realized_gain_h": realized,
                "ripple": ripple_value,
                "sll": sidelobe_level(theta, gain, exclusion),
            }
    except ArtifactError as exc:
        result["invalid"] = str(exc)
        result["feasible"] = False
        return result

    gains = [v["realized_gain_h"] for v in per_frequency.values()]
    thetas = [v["peak_theta"] for v in per_frequency.values()]
    slls = [v["sll"] for v in per_frequency.values() if not math.isnan(v["sll"])]
    ripples = [v["ripple"] for v in per_frequency.values() if v["ripple"] is not None]
    result["metrics"]["s11"] = s11_info
    result["metrics"]["realized_gain_h"] = {
        "value": float(min(gains)),
        "per_frequency": {k: v["realized_gain_h"] for k, v in per_frequency.items()},
        "frequencies_ghz": [float(k) for k in per_frequency],
    }
    result["metrics"]["peak_gain"] = {
        "value": float(max(v["peak_gain"] for v in per_frequency.values())),
        "per_frequency": {k: v["peak_gain"] for k, v in per_frequency.items()},
    }
    result["metrics"]["beam_theta"] = {
        "value": float(max(thetas)),
        "drift_deg": float(max(thetas) - min(thetas)),
        "per_frequency": {k: v["peak_theta"] for k, v in per_frequency.items()},
    }
    if ripples:
        result["metrics"]["ripple"] = {
            "value": float(max(ripples)),
            "per_frequency": {k: v["ripple"] for k, v in per_frequency.items() if v["ripple"] is not None},
        }
    if slls:
        result["metrics"]["sll"] = {
            "value": float(max(slls)),
            "per_frequency": {k: v["sll"] for k, v in per_frequency.items() if not math.isnan(v["sll"])},
        }

    for metric in spec.get("metrics", []):
        kind = metric["kind"]
        if kind not in result["metrics"]:
            continue
        if kind == "s11":
            value = result["metrics"]["s11"]["worst_db"]
        else:
            value = result["metrics"][kind]["value"]
            if kind == "sll" and math.isnan(value):
                result["hard"][metric["id"]] = False
                if metric.get("hard", True):
                    result["unmet"].append(metric["id"])
                continue
        passed = _compare(float(value), metric["operator"], float(metric["target"]))
        result["hard"][metric["id"]] = passed
        if not passed and metric.get("hard", True):
            result["unmet"].append(metric["id"])
    result["feasible"] = not result["unmet"]
    return result


def evaluate(artifacts: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one candidate from its exported artifacts.

    ``artifacts`` keys: ``s11`` (touchstone path), ``theta_cuts`` (mapping of
    phi angle -> far-field CSV), ``phi_cut`` (horizontal CSV) and
    ``frequency_ghz`` (frequency of the far-field exports; defaults to the band
    centre). Realized gain is computed at that frequency from Gain and S11.

    When ``artifacts["farfield"]`` (multi-frequency layout) is present, the
    per-frequency evaluator is used instead.
    """
    if artifacts.get("farfield"):
        return evaluate_frequencies(artifacts, spec)
    evaluation = spec.get("evaluation", {})
    band = tuple(spec["frequency_ghz"][:2])
    exclusion = float(evaluation.get("main_lobe_exclusion_deg", 10))
    horiz_theta = float(evaluation.get("horiz_theta_deg", 90))
    mode = evaluation.get("realized_gain_mode", "gain_plus_mismatch")

    result: dict[str, Any] = {"metrics": {}, "hard": {}, "unmet": [], "invalid": None}
    try:
        freqs, s11_db = read_touchstone(artifacts["s11"])
        s11_info = s11_band(freqs, s11_db, band)
        result["metrics"]["s11"] = s11_info

        gain_freq = float(artifacts.get("frequency_ghz", (band[0] + band[1]) / 2.0))
        s11_at_gain_freq = float(np.interp(gain_freq, freqs, s11_db))

        theta_cuts = {
            str(phi): read_curve_csv(path)
            for phi, path in (artifacts.get("theta_cuts") or {}).items()
        }
        primary_phi = str(int(evaluation.get("theta_cut_phi_deg", [0])[0]))
        theta, gain = theta_cuts.get(primary_phi) or next(iter(theta_cuts.values()))
        peak, peak_theta = peak_and_beam(theta, gain)
        result["metrics"]["peak_gain"] = {"value": peak, "theta_deg": peak_theta}
        result["metrics"]["beam_theta"] = {"value": peak_theta}

        horizontal = horizontal_level(theta, gain, theta_deg=horiz_theta)
        if mode == "gain_plus_mismatch":
            horizontal_realized = float(realized_gain(horizontal, s11_at_gain_freq))
        else:
            horizontal_realized = horizontal
        result["metrics"]["realized_gain_h"] = {"value": horizontal_realized}
        result["metrics"]["sll"] = {"value": sidelobe_level(theta, gain, exclusion)}

        phi_cut = artifacts.get("phi_cut")
        if phi_cut:
            phi, phi_gain = read_curve_csv(phi_cut, x_candidates=("Phi [deg]", "Theta [deg]"))
            result["metrics"]["ripple"] = {"value": ripple(phi, phi_gain)}
    except ArtifactError as exc:
        result["invalid"] = str(exc)
        result["feasible"] = False
        return result

    for metric in spec.get("metrics", []):
        kind = metric["kind"]
        if kind not in result["metrics"]:
            continue
        if kind == "s11":
            value = result["metrics"]["s11"]["worst_db"]
        else:
            value = result["metrics"][kind]["value"]
            if kind == "sll" and math.isnan(value):
                result["unmet"].append({"id": metric["id"], "reason": "sll not computable"})
                if metric.get("hard", True):
                    result["hard"][metric["id"]] = False
                continue
        passed = _compare(float(value), metric["operator"], float(metric["target"]))
        result["hard"][metric["id"]] = passed
        if not passed and metric.get("hard", True):
            result["unmet"].append(metric["id"])

    result["feasible"] = (result["invalid"] is None) and not result["unmet"]
    return result
