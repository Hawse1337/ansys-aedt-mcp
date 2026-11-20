#!/usr/bin/env python3
"""Resident daemon for guarded runs.

Start it explicitly, or let ``submit_guarded_run`` start it on demand:

    python guard_daemon.py --campaign <campaign_dir> [--poll 2] [--once]

The daemon claims queued requests from ``<campaign>/daemon/requests`` and, on
every start, resumes runs that a crashed process left in a non-terminal state
(without re-solving).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from aedt_guard.daemon import DaemonPaths, RunDaemon
from aedt_guard.sim import CampaignRunner
from aedt_guard.store import Store
from aedt_guard.worker_adapter import GuardedAdapter


def _build_adapter(payload: dict) -> GuardedAdapter:
    from worker_client import WorkerClient

    client = WorkerClient()
    return GuardedAdapter(
        client=client,
        pid=payload.get("pid"),
        port=payload.get("port"),
        project_name=str(payload.get("project_name") or ""),
        design_name=str(payload.get("design_name") or ""),
        setup_name=str(payload.get("setup_name") or "Setup1"),
        cuts=list(payload.get("cuts") or ["theta_phi0", "phi_theta90"]),
        worker_timeout=float(payload.get("worker_timeout") or 1800.0),
        sweep_name=payload.get("sweep_name"),
        farfield_setup=payload.get("farfield_setup_name"),
        extra_setups=list(payload.get("extra_setups") or []),
        run_token=payload.get("run_token"),
        solver_settings=payload.get("solver_settings"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded-run daemon")
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--poll", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=None)
    args = parser.parse_args(argv)

    from aedt_guard import load_spec

    paths = DaemonPaths(args.campaign).ensure()
    # Campaign-local projects are resolvable by bare name even when the operator
    # starts the daemon without the MCP environment.
    local_projects = paths.root / "projects"
    if local_projects.is_dir():
        existing = os.environ.get("AEDT_GUARD_SEARCH_DIRS", "")
        entries = [entry for entry in existing.split(os.pathsep) if entry]
        if str(local_projects) not in entries:
            entries.append(str(local_projects))
            os.environ["AEDT_GUARD_SEARCH_DIRS"] = os.pathsep.join(entries)
    if not paths.acquire_singleton():
        print("[daemon] another daemon already owns this campaign", file=sys.stderr, flush=True)
        return 0
    print(f"[daemon] serving {paths.root} (pid {__import__('os').getpid()})", flush=True)
    try:
        daemon = RunDaemon(
            args.campaign,
            store_factory=Store,
            spec_loader=load_spec,
            adapter_factory=_build_adapter,
            runner_factory=CampaignRunner,
        )
        daemon.serve(poll_interval_s=args.poll, once=args.once,
                     max_seconds=args.max_seconds)
    finally:
        paths.release_singleton()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

