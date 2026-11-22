# -*- coding: utf-8 -*-
"""Background watcher for pending builder approvals.

The GUI is not always in front.  This watcher notices that the selected
candidate script has diverged from the approved copy (new SHA-256), that the
approval sidecar changed, or that the candidate declares parameters/enums not
present in the allowlist, and calls the notification callback once per change.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from builder_schema import detect_candidate_schema, detect_suggestions


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecar(candidate: Path) -> Path | None:
    candidates = [
        candidate.with_name(candidate.stem + ".approval-request.json"),
        candidate.with_name(candidate.name + ".approval-request.json"),
    ]
    return next((path for path in candidates if path.is_file()), None)


def _request_metadata(candidate: Path) -> tuple[dict[str, Any] | None, Path | None]:
    path = _sidecar(candidate)
    if path is None:
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (data if isinstance(data, dict) else None), path
    except Exception:  # noqa: BLE001
        return None, path


class ApprovalWatcher:
    """Poll the selected candidate and notify exactly once per pending change."""

    def __init__(
        self,
        allowlist_getter: Callable[[], Path],
        builder_getter: Callable[[], str | None],
        candidate_getter: Callable[[], str],
        enabled_getter: Callable[[], bool],
        notify: Callable[[str, str], None],
        log: Callable[[str], None],
        *,
        interval: float = 4.0,
    ) -> None:
        self._allowlist_getter = allowlist_getter
        self._builder_getter = builder_getter
        self._candidate_getter = candidate_getter
        self._enabled_getter = enabled_getter
        self._notify = notify
        self._log = log
        self._interval = max(2.0, float(interval))
        self._stop = threading.Event()
        self._seen: set[str] = set()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="approval-watcher")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def check_once(self) -> None:
        if not self._enabled_getter():
            return
        builder_id = (self._builder_getter() or "").strip()
        raw = (self._candidate_getter() or "").strip()
        if not builder_id or not raw:
            return
        candidate = Path(raw).expanduser()
        if not candidate.is_file():
            return
        try:
            digest = _sha256(candidate)
            allowlist_path = Path(self._allowlist_getter())
            data = json.loads(allowlist_path.read_text(encoding="utf-8"))
            entry = data["builders"][builder_id]
            approved = str(entry.get("sha256", "")).lower()
            params = entry.get("params") or {}
        except Exception:  # noqa: BLE001
            return

        if digest != approved:
            key = "hash:%s:%s" % (candidate.resolve(), digest)
            if key not in self._seen:
                self._seen.add(key)
                meta, _path = _request_metadata(candidate)
                reason = str((meta or {}).get("reason") or "").strip()
                source = str((meta or {}).get("platform") or "").strip()
                body = "%s 的候选脚本已更新，需要重新审批。\n%s" % (builder_id, candidate.name)
                if source:
                    body += "\n来源：%s" % source
                if reason:
                    body += "\n说明：%s" % reason
                body += "\nSHA-256：%s…" % digest[:16]
                self._notify("AEDT Builder 待审批", body)
                self._log("[notify] pending candidate hash %s" % digest[:16])
            return

        # Approved script but the allowlist schema does not declare everything
        # the script accepts (e.g. a new design/geometry parameter).
        try:
            detected = detect_candidate_schema(candidate, params)
            suggestions = detect_suggestions(params, detected)
            missing = list((suggestions.get("new") or {}).keys())
            enum_add = list((suggestions.get("enum_additions") or {}).keys())
            if missing or enum_add:
                key = "schema:%s:%s:%s" % (
                    candidate.resolve(),
                    digest,
                    ",".join(sorted(missing + enum_add)),
                )
                if key not in self._seen:
                    self._seen.add(key)
                    details = []
                    if missing:
                        details.append("缺参数：" + ", ".join(missing))
                    if enum_add:
                        details.append("缺枚举：" + ", ".join(enum_add))
                    self._notify(
                        "AEDT Builder 参数表待更新",
                        "%s 已批准，但参数表没有声明：\n%s"
                        % (builder_id, "；".join(details)),
                    )
                    self._log("[notify] schema mismatch %s" % ", ".join(missing + enum_add))
        except Exception:  # noqa: BLE001
            pass

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 - watcher must never kill the GUI
                time.sleep(self._interval)
