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
        data = json.loads(path.read_text(encoding="utf-8-sig"))
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
        requests_getter: Callable[[], Path] | None = None,
        interval: float = 4.0,
    ) -> None:
        self._allowlist_getter = allowlist_getter
        self._builder_getter = builder_getter
        self._candidate_getter = candidate_getter
        self._enabled_getter = enabled_getter
        self._notify = notify
        self._log = log
        self._requests_getter = requests_getter
        self._interval = max(2.0, float(interval))
        self._stop = threading.Event()
        self._seen: set[str] = set()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="approval-watcher")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _request_rows(self) -> list[dict[str, Any]]:
        if self._requests_getter is None:
            return []
        root = Path(self._requests_getter())
        if not root.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for path in root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("status") == "pending":
                data["request_path"] = str(path)
                rows.append(data)
        return rows

    def check_once(self) -> None:
        if not self._enabled_getter():
            return
        pending = self._request_rows()
        pending_candidates: set[str] = set()
        for request in pending:
            request_id = str(request.get("request_id") or "")
            if not request_id:
                continue
            candidate_text = str(request.get("candidate") or "").strip()
            if candidate_text:
                try:
                    pending_candidates.add(str(Path(candidate_text).expanduser().resolve()).lower())
                except OSError:
                    pass
            key = "request:%s" % request_id
            if key in self._seen:
                continue
            self._seen.add(key)
            builder = str(request.get("builder_id") or "")
            request_type = str(request.get("request_type") or "builder_change")
            body = "%s 有一个待审批的 %s 请求。\n%s" % (
                builder,
                "参数表更新" if request_type == "schema_only" else "builder 新版本",
                candidate_text or "(无候选文件)",
            )
            reason = str(request.get("reason") or "").strip()
            if reason:
                body += "\n原因：%s" % reason
            body += "\nrequest_id：%s" % request_id[:12]
            self._notify("AEDT Builder 审批请求", body)
            self._log("[notify] approval request %s" % request_id[:12])

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
            data = json.loads(allowlist_path.read_text(encoding="utf-8-sig"))
            entry = data["builders"][builder_id]
            approved = str(entry.get("sha256", "")).lower()
            params = entry.get("params") or {}
        except Exception:  # noqa: BLE001
            return

        if digest != approved:
            if str(candidate.resolve()).lower() in pending_candidates:
                return
            key = "hash:%s:%s" % (candidate.resolve(), digest)
            if key not in self._seen:
                self._seen.add(key)
                meta, _path = _request_metadata(candidate)
                reason = str((meta or {}).get("reason") or "").strip()
                source = str((meta or {}).get("platform") or "").strip()
                body = (
                    "%s 的候选脚本已变化，但当前没有对应的待审批请求。\n"
                    "如要审批，请先调用 request_builder_approval 提交；"
                    "新 builder 使用 request_type='new_builder'。\n%s"
                    % (builder_id, candidate.name)
                )
                if source:
                    body += "\n来源：%s" % source
                if reason:
                    body += "\n说明：%s" % reason
                body += "\nSHA-256：%s…" % digest[:16]
                self._notify("AEDT Builder 候选已变化", body)
                self._log("[notify] candidate changed, no pending request %s" % digest[:16])
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
