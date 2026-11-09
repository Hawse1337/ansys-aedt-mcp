"""Minimal run/metrics store (SQLite) for the guarded workflow.

The store is deliberately file-backed and dependency-free so a campaign can be
archived, copied or inspected with plain SQL. It records every run, its spec
version, parameters, artifacts, status and evaluated metrics.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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
    notes TEXT
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
        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def add_spec_version(self, spec: dict[str, Any], reason: str = "", approver: str = "") -> None:
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
    ) -> None:
        stamp = _now()
        self.connection.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
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
            ),
        )
        self.connection.commit()

    def set_status(self, run_id: str, status: str, *, notes: str | None = None) -> None:
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
        self.connection.executemany("INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?)", rows)
        self.connection.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
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

    def counts(self) -> dict[str, int]:
        runs = self.connection.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        metrics = self.connection.execute("SELECT COUNT(*) AS c FROM metrics").fetchone()["c"]
        specs = self.connection.execute("SELECT COUNT(*) AS c FROM spec_versions").fetchone()["c"]
        return {"runs": int(runs), "metrics": int(metrics), "spec_versions": int(specs)}
