# -*- coding: utf-8 -*-
"""Live smoke for the v1.5 guarded-workflow commands (disposable sandbox).

A half-wave dipole in an open region is used instead of a waveguide case:
far-field tooling needs a *radiating* design (radiation boundary + infinite
sphere). Only a NEW project under D:\\AgentAEDT_Collab\\sandbox\\smoke_v15 is
touched.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

DEV = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(DEV))

import aedt_project_guard  # noqa: E402

aedt_project_guard.install()

from aedt_target import AedtTarget  # noqa: E402
from pyaedt_backend import PyAedtBackend  # noqa: E402

import mcp_server  # noqa: E402

PID = int(os.environ["SMOKE_PID"])
SANDBOX = Path(r"D:\AgentAEDT_Collab\sandbox\smoke_v15")
SANDBOX.mkdir(parents=True, exist_ok=True)

report: dict = {"steps": []}


def step(name, fn):
    try:
        value = fn()
        detail = json.loads(json.dumps(value, default=str))
        report["steps"].append({"name": name, "ok": True, "detail": detail})
        print("STEP %s OK %s" % (name, json.dumps(detail)[:500]), flush=True)
        return value
    except Exception as exc:  # noqa: BLE001
        report["steps"].append({"name": name, "ok": False, "error": repr(exc)[:400]})
        print("STEP %s FAIL %s" % (name, repr(exc)[:400]), flush=True)
        return None


def sandbox_hfss_factory(**kwargs):
    from ansys.aedt.core import Hfss

    path = SANDBOX / "dipole_smoke.aedt"
    path.unlink(missing_ok=True)
    mcp_server.clear_stale_lock(str(path))
    import shutil

    shutil.rmtree(Path(str(path) + "results"), ignore_errors=True)
    extra = {k: v for k, v in kwargs.items() if k != "project"}
    return aedt_project_guard.create_project(Hfss, str(path), **extra)


backend = PyAedtBackend(version="2026.1", hfss_factory=sandbox_hfss_factory)
target = AedtTarget("pid", PID)
app = None


def build():
    """Build a half-wave dipole at 2.5 GHz with open region + infinite sphere."""
    global app
    result = backend.execute(target, "create_hfss_design", {
        "project_name": "dipole_smoke",
        "design_name": "Dipole",
        "solution_type": "DrivenModal",
    })
    app = next(iter(backend._apps.values()))
    arm_r, arm_len, gap = 0.5, 30.0, 1.0
    app.modeler.create_cylinder(orientation="Z", origin=[0, 0, gap], radius=arm_r,
                                height=arm_len, name="arm_up", material="pec")
    app.modeler.create_cylinder(orientation="Z", origin=[0, 0, -gap - arm_len], radius=arm_r,
                                height=arm_len, name="arm_dn", material="pec")
    port = app.modeler.create_polyline(
        points=[[-5.0, 0.0, -gap], [5.0, 0.0, -gap], [5.0, 0.0, gap], [-5.0, 0.0, gap],
                [-5.0, 0.0, -gap]],
        name="port_sheet", material="vacuum", cover_surface=True,
    )
    app.lumped_port(assignment="port_sheet", integration_line=[[0, 0, -gap], [0, 0, gap]],
                    impedance=73, name="P1", renormalize=False)
    region = app.create_open_region(frequency="2.5GHz")
    sphere = app.insert_infinite_sphere(phi_start=0, phi_stop=180, theta_start=0, theta_stop=180)
    setup = app.create_setup("Setup1")
    setup.props["Frequency"] = "2.5GHz"
    setup.props["MaximumPasses"] = 6
    setup.props["MinimumPasses"] = 2
    setup.props["MinimumConvergedPasses"] = 1
    setup.props["MaxDeltaS"] = 0.02
    setup.update()
    app.create_linear_count_sweep(setup="Setup1", unit="GHz", start_frequency=2.0,
                                  stop_frequency=3.0, num_of_freq_points=11,
                                  name="Sweep2to3", sweep_type="Interpolating", save_fields=False)
    saved = bool(app.save_project())
    return {
        "create_keys": sorted(result.keys()),
        "open_region": region,
        "sphere": sphere,
        "field_setup_names": list(app.field_setup_names),
        "saved": saved,
    }


def preflight():
    return backend._preflight_design(app, target, {
        "project_name": "dipole_smoke",
        "design_name": "Dipole",
        "setup_name": "Setup1",
        "validate_attempts": 3,
    })


def solve():
    started = bool(app.analyze_setup(name="Setup1", cores=2, blocking=True))
    return {"analyze_setup_returned": started}


def status():
    return {"running": bool(app.are_there_simulations_running)}


def finalize():
    return backend._finalize_solve(app, target, {
        "project_name": "dipole_smoke",
        "design_name": "Dipole",
        "setups": ["Setup1"],
    })


def farfield():
    return backend._export_farfield_report(app, target, {
        "project_name": "dipole_smoke",
        "design_name": "Dipole",
        "setup_name": "Setup1",
        "output_dir": str(SANDBOX / "exports"),
    })


step("build_dipole_plus_sphere", build)
step("preflight", preflight)
step("solve", solve)
time.sleep(2)
step("status_after_solve", status)
step("finalize", finalize)
step("farfield", farfield)

paths = sorted((SANDBOX / "exports").glob("*.csv"))
report["verify_artifacts"] = mcp_server.verify_artifacts([str(p) for p in paths], min_rows=1)

out = SANDBOX / "smoke_report.json"
out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print("REPORT %s" % out, flush=True)
try:
    with open(out, "rb") as handle:
        os.fsync(handle.fileno())
except Exception:
    pass
os._exit(0)
