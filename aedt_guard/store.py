"""Minimal run/metrics store (SQLite) for the guarded workflow.

The store is deliberately file-backed and dependency-free so a campaign can be
archived, copied or inspected with plain SQL. It records every run, its spec
version, parameters, artifacts, status and evaluated metrics.

The guarded runner writes from a background thread while MCP tool calls read
from the request thread, so the connection is opened with
``check_same_thread=False`` and every access is serialised by a re-entrant
lock. WAL mode is enabled so readers are not blocked by the runner's writes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    spec_version INTEGER NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL,
    artifact_dir TEXT,
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    notes TEXT,
    cache_key TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL,
    unit TEXT,
    passed INTEGER,
    detail_json TEXT,
    PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS spec_versions (
    project TEXT NOT NULL,
    version INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    reason TEXT,
    approver TEXT,
    created_utc TEXT NOT NULL,
    PRIMARY KEY (project, version)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.executescript(SCHEMA)
            self._migrate()
            self.connection.commit()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runs)")}
        if "cache_key" not in columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN cache_key TEXT")

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def add_spec_version(self, spec: dict[str, Any], reason: str = "", approver: str = "") -> None:
        with self._lock:
            self.connection.execute(
                "INSERT OR REPLACE INTO spec_versions VALUES (?,?,?,?,?,?)",
                (
                    spec["project"],
                    int(spec["version"]),
                    json.dumps(spec, ensure_ascii=False),
                    reason,
                    approver,
                    _now(),
                ),
            )
            self.connection.commit()

    def add_run(
        self,
        run_id: str,
        project: str,
        spec_version: int,
        params: dict[str, Any],
        *,
        status: str = "submitted",
        artifact_dir: str | None = None,
        notes: str | None = None,
        cache_key: str | None = None,
    ) -> None:
        stamp = _now()
        with self._lock:
            self.connection.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    project,
                    int(spec_version),
                    json.dumps(params, ensure_ascii=False),
                    status,
                    artifact_dir,
                    stamp,
                    stamp,
                    notes,
                    cache_key,
                ),
            )
            self.connection.commit()

    def set_status(self, run_id: str, status: str, *, notes: str | None = None) -> None:
        with self._lock:
            self.connection.execute(
                "UPDATE runs SET status=?, updated_utc=?, notes=COALESCE(?, notes) WHERE run_id=?",
                (status, _now(), notes, run_id),
            )
            self.connection.commit()

    def add_metrics(self, run_id: str, metrics: dict[str, dict[str, Any]],
                    passed: dict[str, bool] | None = None) -> None:
        rows = []
        for name, payload in metrics.items():
            value = payload.get("value") if isinstance(payload, dict) else payload
            unit = payload.get("unit") if isinstance(payload, dict) else None
            rows.append(
                (
                    run_id,
                    name,
                    float(value) if isinstance(value, (int, float)) else None,
                    unit,
                    None if passed is None else int(bool(passed.get(name))),
                    json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else None,
                )
            )
        with self._lock:
            self.connection.executemany("INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?)", rows)
            self.connection.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            payload = dict(row)
            payload["params"] = json.loads(payload.pop("params_json"))
            payload["metrics"] = {
                item["name"]: dict(item)
                for item in self.connection.execute("SELECT * FROM metrics WHERE run_id=?", (run_id,))
            }
        return payload

    def list_runs(self, project: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            if project is None:
                rows = self.connection.execute(
                    "SELECT * FROM runs ORDER BY created_utc DESC LIMIT ?", (int(limit),)
                )
            else:
                rows = self.connection.execute(
                    "SELECT * FROM runs WHERE project=? ORDER BY created_utc DESC LIMIT ?",
                    (project, int(limit)),
                )
            return [dict(row) for row in rows]

    def best_feasible(self, project: str, metric: str, maximize: bool = True) -> dict[str, Any] | None:
        """Best run whose metric row passed, ordered by the metric value."""
        direction = "DESC" if maximize else "ASC"
        with self._lock:
            row = self.connection.execute(
                "SELECT r.run_id, r.params_json, m.value FROM runs r "
                "JOIN metrics m ON m.run_id = r.run_id "
                "WHERE r.project=? AND m.name=? AND m.passed=1 AND m.value IS NOT NULL "
                f"ORDER BY m.value {direction} LIMIT 1",
                (project, metric),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "value": row["value"],
            "params": json.loads(row["params_json"]),
        }

    def find_completed_by_cache_key(self, project: str, cache_key: str) -> dict[str, Any] | None:
        """Return the newest COMPLETED run with the same cache key, if any."""
        if not cache_key:
            return None
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM runs WHERE project=? AND cache_key=? AND status='COMPLETED' "
                "ORDER BY created_utc DESC LIMIT 1",
                (project, cache_key),
            ).fetchone()
            if row is None:
                return None
            payload = dict(row)
            payload["params"] = json.loads(payload.pop("params_json"))
            payload["metrics"] = {
                item["name"]: dict(item)
                for item in self.connection.execute(
                    "SELECT * FROM metrics WHERE run_id=?", (payload["run_id"],)
                )
            }
        return payload

    def counts(self) -> dict[str, int]:
        with self._lock:
            runs = self.connection.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
            metrics = self.connection.execute("SELECT COUNT(*) AS c FROM metrics").fetchone()["c"]
            specs = self.connection.execute("SELECT COUNT(*) AS c FROM spec_versions").fetchone()["c"]
        return {"runs": int(runs), "metrics": int(metrics), "spec_versions": int(specs)}
