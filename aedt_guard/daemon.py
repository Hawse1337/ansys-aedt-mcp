"""Resident run daemon primitives: filesystem queue, heartbeat, singleton, recovery.

The daemon owns guarded runs so that an MCP restart does not strand them. It
communicates through plain files inside the campaign directory (no sockets, no
ports), which keeps it inspectable and robust across crashes:

    <campaign>/daemon/requests/<run_id>.json   queued run request
    <campaign>/daemon/claims/<run_id>.json     claim marker (one daemon per run)
    <campaign>/daemon/heartbeat.json           {pid, started_utc, updated_utc, current_run}
    <campaign>/daemon/daemon.lock              singleton lock (pid + age)
    <campaign>/daemon/daemon.log               stdout/stderr of the daemon
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "BUDGET_EXCEEDED", "RECOVERY_REQUIRED"}
HEARTBEAT_FRESH_S = 30.0
# A resident daemon owns the campaign lock for as long as it lives. The lock is
# only taken over when its owner is provably dead, or after this long without a
# heartbeat refresh (a stuck process must not block the campaign forever).
LOCK_STALE_S = 3600.0
RECOVERY_WAIT_S = 300.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _pid_alive(pid: int) -> bool:
    try:
        import psutil

        return bool(psutil.pid_exists(int(pid)))
    except Exception:  # noqa: BLE001
        return False


class DaemonPaths:
    def __init__(self, campaign_dir: str | Path):
        self.root = Path(campaign_dir).expanduser().resolve()
        self.daemon_dir = self.root / "daemon"
        self.requests = self.daemon_dir / "requests"
        self.claims = self.daemon_dir / "claims"
        self.heartbeat = self.daemon_dir / "heartbeat.json"
        self.lock = self.daemon_dir / "daemon.lock"
        self.log = self.daemon_dir / "daemon.log"

    def ensure(self) -> "DaemonPaths":
        self.requests.mkdir(parents=True, exist_ok=True)
        self.claims.mkdir(parents=True, exist_ok=True)
        self.daemon_dir.mkdir(parents=True, exist_ok=True)
        return self

    # -------------------------------------------------------------- requests
    def request_path(self, run_id: str) -> Path:
        return self.requests / f"{run_id}.json"

    def write_request(self, payload: dict[str, Any]) -> Path:
        self.ensure()
        path = self.request_path(str(payload["run_id"]))
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def list_requests(self) -> list[tuple[str, dict[str, Any]]]:
        if not self.requests.is_dir():
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        for path in sorted(self.requests.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            out.append((path.stem, payload))
        return out

    def drop_request(self, run_id: str) -> None:
        try:
            self.request_path(run_id).unlink()
        except FileNotFoundError:
            pass

    # ---------------------------------------------------------------- claims
    def claim(self, run_id: str) -> bool:
        self.ensure()
        path = self.claims / f"{run_id}.json"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "at": _now()}))
            return True
        except FileExistsError:
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                info = {}
            owner = int(info.get("pid") or 0)
            if owner and _pid_alive(owner):
                return False
            path.write_text(json.dumps({"pid": os.getpid(), "at": _now()}), encoding="utf-8")
            return True

    def release(self, run_id: str) -> None:
        try:
            (self.claims / f"{run_id}.json").unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------ heartbeat
    def write_heartbeat(self, *, current_run: str | None = None) -> None:
        self.ensure()
        payload = {
            "pid": os.getpid(),
            "started_utc": getattr(self, "_started", None) or _now(),
            "updated_utc": _now(),
            "current_run": current_run,
            "updated_ts": _now_ts(),
        }
        if not getattr(self, "_started", None):
            self._started = payload["started_utc"]
        self.heartbeat.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                  encoding="utf-8")

    def read_heartbeat(self) -> dict[str, Any] | None:
        if not self.heartbeat.is_file():
            return None
        try:
            return json.loads(self.heartbeat.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def alive(self, max_age_s: float = HEARTBEAT_FRESH_S) -> dict[str, Any]:
        info = self.read_heartbeat()
        if not info:
            return {"alive": False, "reason": "no_heartbeat", "pid": None}
        pid = int(info.get("pid") or 0)
        updated = float(info.get("updated_ts") or 0.0)
        age = _now_ts() - updated
        if not pid or not _pid_alive(pid):
            return {"alive": False, "reason": "owner_dead", "pid": pid, "age_s": round(age, 1)}
        if age > max_age_s:
            return {"alive": False, "reason": "heartbeat_stale", "pid": pid, "age_s": round(age, 1)}
        return {"alive": True, "reason": "ok", "pid": pid, "age_s": round(age, 1),
                "current_run": info.get("current_run")}

    # -------------------------------------------------------------- singleton
    def acquire_singleton(self) -> bool:
        """Atomically claim the campaign lock.

        ``O_EXCL`` makes the claim race-free: with four daemons started at once
        exactly one creates the file and the others observe ``FileExistsError``.
        A lock whose owner is dead (or older than ``LOCK_STALE_S``) is taken over.
        """
        self.ensure()
        payload = json.dumps({"pid": os.getpid(), "at": _now_ts()})
        for _attempt in range(2):
            try:
                handle = os.open(str(self.lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                try:
                    info = json.loads(self.lock.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    info = {}
                owner = int(info.get("pid") or 0)
                at = float(info.get("at") or 0.0)
                stale = not owner or not _pid_alive(owner) or (_now_ts() - at) >= LOCK_STALE_S
                if not stale:
                    return False
                try:
                    self.lock.unlink()
                except FileNotFoundError:
                    pass
                continue
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                return True
            except OSError:
                return False
        return False

    def release_singleton(self) -> None:
        try:
            info = json.loads(self.lock.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            info = {}
        if int(info.get("pid") or 0) in (0, os.getpid()):
            try:
                self.lock.unlink()
            except FileNotFoundError:
                pass


class RunDaemon:
    """Process one queued request at a time and recover interrupted runs."""

    def __init__(
        self,
        campaign_dir: str | Path,
        *,
        store_factory: Callable[[Path], Any],
        spec_loader: Callable[[Path], dict[str, Any]],
        adapter_factory: Callable[[dict[str, Any]], Any],
        runner_factory: Callable[..., Any],
        project: str | None = None,
    ):
        self.paths = DaemonPaths(campaign_dir).ensure()
        self.store_factory = store_factory
        self.spec_loader = spec_loader
        self.adapter_factory = adapter_factory
        self.runner_factory = runner_factory
        self.project = project

    # ---------------------------------------------------------------- helpers
    def _open_store(self):
        return self.store_factory(self.paths.root / "campaign.sqlite")

    @staticmethod
    def _last_stage(detail: dict[str, Any]) -> str:
        try:
            note = json.loads(detail.get("notes") or "[]")
        except json.JSONDecodeError:
            note = []
        return next((entry.get("stage") for entry in reversed(note) if entry.get("stage")),
                    str(detail.get("status") or "CREATED"))

    @staticmethod
    def _wait_if_solving(adapter: Any, timeout_s: float, poll_interval_s: float) -> None:
        """A crashed SOLVING run may still be solving in AEDT; wait for idle.

        Capped at ``RECOVERY_WAIT_S``: a killed AEDT can leave a sticky
        ``running`` flag, and waiting the full run timeout would starve every
        other queued request. Finalize later reports the real evidence.
        """
        wait = getattr(adapter, "wait", None)
        if not callable(wait):
            return
        try:
            wait(min(float(timeout_s), RECOVERY_WAIT_S), max(poll_interval_s, 2.0))
        except Exception:  # noqa: BLE001 - finalize will report the real evidence
            pass

    def recover(self) -> list[dict[str, Any]]:
        """Resume runs that were left in a non-terminal state by a crash."""
        recovered = []
        store = self._open_store()
        try:
            runs = store.list_runs(self.project, limit=50) if hasattr(store, "list_runs") else []
            for run in runs:
                status = str(run.get("status") or "")
                if status in TERMINAL_STATUSES or status == "CREATED":
                    continue
                if not self.paths.claim(run["run_id"]):
                    continue
                try:
                    detail = store.get_run(run["run_id"]) or {}
                    spec_path = self.paths.root / "design_spec.json"
                    spec = self.spec_loader(spec_path)
                    last = self._last_stage(detail)
                    artifact_dir = Path(detail.get("artifact_dir") or
                                        self.paths.root / "runs" / run["run_id"] / "artifacts")
                    adapter = self.adapter_factory({
                        "run_id": run["run_id"],
                        "project_name": (detail.get("params") or {}).get("project_name"),
                        "design_name": (detail.get("params") or {}).get("design_name"),
                        "setup_name": (detail.get("params") or {}).get("setup_name") or "Setup1",
                        "pid": (detail.get("params") or {}).get("pid"),
                        "port": (detail.get("params") or {}).get("port"),
                        "cuts": ["theta_phi0", "phi_theta90"],
                        "sweep_name": None,
                        "frequencies_ghz": None,
                        "solver_settings": None,
                    })
                    if last == "SOLVING":
                        self._wait_if_solving(adapter, 1800.0, 5.0)
                    runner = self.runner_factory(
                        store=store, adapter=adapter, spec=spec, run_id=run["run_id"],
                        artifact_dir=artifact_dir,
                    )
                    outcome = runner.resume(last)
                    recovered.append({"run_id": run["run_id"], "resumed_from": last,
                                      "status": outcome})
                finally:
                    self.paths.release(run["run_id"])
        finally:
            store.close()
        return recovered

    def poll_once(self) -> dict[str, Any] | None:
        pending = self.paths.list_requests()
        if not pending:
            return None
        run_id, payload = pending[0]
        if not self.paths.claim(run_id):
            return {"run_id": run_id, "status": "skipped_claimed"}
        self.paths.write_heartbeat(current_run=run_id)
        store = self._open_store()
        try:
            spec = self.spec_loader(self.paths.root / "design_spec.json")
            adapter = self.adapter_factory(payload)
            artifact_dir = self.paths.root / "runs" / run_id / "artifacts"
            detail = store.get_run(run_id) or {}
            status = str(detail.get("status") or "CREATED")
            if status in TERMINAL_STATUSES:
                self.paths.drop_request(run_id)
                return {"run_id": run_id, "status": status, "skipped_terminal": True}
            resumable = status != "CREATED"
            runner = self.runner_factory(
                store=store,
                adapter=adapter,
                spec=spec,
                run_id=run_id,
                artifact_dir=artifact_dir,
                params=payload.get("params") or {},
                cuts=payload.get("cuts") or ["theta_phi0", "phi_theta90"],
                timeout_s=float(payload.get("timeout_s") or 3600.0),
                poll_interval_s=float(payload.get("poll_interval_s") or 5.0),
                frequency_ghz=payload.get("frequency_ghz"),
                frequencies=payload.get("frequencies_ghz"),
            )
            if resumable:
                last = self._last_stage(detail)
                if last == "SOLVING":
                    self._wait_if_solving(adapter, float(payload.get("timeout_s") or 3600.0),
                                          float(payload.get("poll_interval_s") or 5.0))
                outcome = runner.resume(last)
                return {"run_id": run_id, "status": outcome, "resumed_from": last}
            outcome = runner.run()
            return {"run_id": run_id, "status": outcome}
        finally:
            store.close()
            self.paths.release(run_id)
            self.paths.drop_request(run_id)

    def serve(self, poll_interval_s: float = 2.0, once: bool = False,
              max_seconds: float | None = None) -> None:
        started = time.monotonic()
        while True:
            self.paths.write_heartbeat()
            for item in self.recover():
                print(f"[daemon] recovered {item}", flush=True)
            result = self.poll_once()
            if result:
                print(f"[daemon] {result}", flush=True)
                continue
            if once:
                return
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                return
            time.sleep(poll_interval_s)





