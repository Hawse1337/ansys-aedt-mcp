# -*- coding: utf-8 -*-
"""AEDT Builder Approver — operator GUI for approved builders.

Double-click the built .exe (or run this file) to:
  * compare a candidate builder with the currently approved copy;
  * see the request's platform/session metadata (sidecar next to the candidate);
  * approve the candidate and update the allowlist atomically;
  * edit the parameter schema (e.g. add a new enum value).

Operator-only tool; it does not execute model code.
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import getpass
import hashlib
import json
import os
import shutil
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from approval_inbox import decide_request as decide_approval_request
from approval_inbox import list_pending as list_pending_requests
from approval_inbox import pending_dir as approval_pending_dir
from approval_watch import ApprovalWatcher
from builder_schema import detect_candidate_schema, detect_suggestions
from notify import windows_toast
from optimization_view import gate_for_campaign, is_optimizer_run, read_recent
from schema_editor import SchemaEditorDialog

GUARD_DIR = Path(r"C:\ProgramData\ansys-aedt-mcp")
DEFAULT_ALLOWLIST = GUARD_DIR / "builders.local.json"
DEFAULT_APPROVED_DIR = GUARD_DIR / "approved"

BG = "#F1F5F9"
CARD = "#FFFFFF"
NAVY = "#0B1220"
NAVY_SOFT = "#111C33"
TEAL = "#0F766E"
TEAL_HOVER = "#115E59"
ACCENT = "#F59E0B"
TEXT = "#0F172A"
MUTED = "#64748B"
BORDER = "#E2E8F0"


def asset_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "assets" / name


def apply_modern_style(root: tk.Tk) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:  # noqa: BLE001
        pass
    style.configure(".", font=("Segoe UI", 10))
    style.configure("TFrame", background=CARD)
    style.configure("Bg.TFrame", background=BG)
    style.configure("TLabel", background=CARD, foreground=TEXT)
    style.configure("Muted.TLabel", background=CARD, foreground=MUTED)
    style.configure("Header.TLabel", background=NAVY, foreground="#F8FAFC",
                    font=("Segoe UI Semibold", 20))
    style.configure("HeaderSub.TLabel", background=NAVY, foreground="#94A3B8",
                    font=("Segoe UI", 10))
    style.configure("Badge.TLabel", background="#132B3A", foreground="#5EEAD4",
                    font=("Segoe UI Semibold", 9), padding=(10, 5))
    style.configure("TLabelframe", background=CARD, bordercolor=BORDER,
                    relief="solid", borderwidth=1, padding=12)
    style.configure("TLabelframe.Label", background=CARD, foreground=MUTED,
                    font=("Segoe UI Semibold", 9))
    style.configure("TButton", background="#E2E8F0", foreground=TEXT,
                    borderwidth=0, padding=(12, 7), font=("Segoe UI Semibold", 9))
    style.map("TButton", background=[("active", "#CBD5E1"), ("disabled", "#EDF2F7")])
    style.configure("Accent.TButton", background=TEAL, foreground="#FFFFFF",
                    borderwidth=0, padding=(15, 8), font=("Segoe UI Semibold", 10))
    style.map("Accent.TButton", background=[("active", TEAL_HOVER), ("disabled", "#94A3B8")])
    style.configure("Secondary.TButton", background="#EEF2F7", foreground=TEXT,
                    borderwidth=0, padding=(12, 7), font=("Segoe UI Semibold", 9))
    style.map("Secondary.TButton", background=[("active", "#DDE5EE")])
    style.configure("TEntry", fieldbackground="#FFFFFF", bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=6)
    style.configure("TCombobox", fieldbackground="#FFFFFF", padding=4)
    style.configure("TCheckbutton", background=CARD, foreground=TEXT)
    style.map("TCheckbutton", background=[("active", CARD)])



def ui_config_path() -> Path:
    override = os.environ.get("AEDT_APPROVER_UI_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "ansys-aedt-mcp" / "builder_approver_ui.json"


def load_ui_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - missing/corrupt UI state is harmless
        return {}


def save_ui_config(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(prefix="approver-ui-", suffix=".json", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - UI convenience must never block approval
        pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_allowlist(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_allowlist(path: Path, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix="builders-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def check_builder(entry: dict) -> tuple[bool, str, str, str]:
    script = Path(entry.get("script", ""))
    expected = str(entry.get("sha256", "")).lower()
    if not script.is_file():
        return False, str(script), expected, "(missing)"
    actual = sha256(script)
    return actual == expected, str(script), expected, actual


def _candidate_sidecars(candidate: Path) -> list[Path]:
    return [
        candidate.with_name(candidate.stem + ".approval-request.json"),
        candidate.with_name(candidate.name + ".approval-request.json"),
        GUARD_DIR / "approval_requests" / (candidate.stem + ".json"),
        candidate.parent / "approval-request.json",
    ]


def load_request_metadata(candidate: Path) -> tuple[dict | None, Path | None, bool]:
    """Return (metadata, sidecar_path, stale_hash)."""
    if not candidate.is_file():
        return None, None, False
    digest = sha256(candidate)
    for path in _candidate_sidecars(candidate):
        if not path.is_file():
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(meta, dict):
            continue
        expected = str(meta.get("candidate_sha256", "")).lower()
        stale = bool(expected and expected != digest)
        return meta, path, stale
    return None, None, False


def format_source(meta: dict | None) -> str:
    if not meta:
        return "未提供来源（可点“编辑来源信息”补充）"
    platform = str(meta.get("platform") or "未知平台")
    session_id = str(meta.get("session_id") or "")
    title = str(meta.get("session_title") or "")
    parts = [platform]
    if session_id:
        parts.append("会话 " + session_id)
    if title:
        parts.append(title)
    return " / ".join(parts)


def request_time(meta: dict | None, candidate: Path) -> str:
    if meta and meta.get("requested_at"):
        return str(meta["requested_at"])
    return datetime.datetime.fromtimestamp(candidate.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


class ApproverApp:
    def __init__(self, root: tk.Tk, allowlist_path: Path, approved_dir: Path) -> None:
        self.root = root
        self.allowlist_path = allowlist_path
        self.approved_dir = approved_dir
        self.data: dict = {}
        self.current_id: str | None = None
        self.request_meta: dict | None = None
        self.request_meta_path: Path | None = None
        self.request_meta_stale = False
        self.ui_config_path = ui_config_path()
        self.ui_config = load_ui_config(self.ui_config_path)
        self.notifications_var = tk.BooleanVar(value=bool(self.ui_config.get("notifications", True)))
        self._watch_candidate = ""
        self.pending_requests: list[dict] = []
        self._active_request: dict | None = None
        self.optimization_rows: list[dict] = []
        self.watcher: ApprovalWatcher | None = None

        root.title("AEDT Builder Approver")
        root.geometry("1180x820")
        root.minsize(980, 700)
        root.configure(bg=BG)
        apply_modern_style(root)
        self._build_header(root)

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Allowlist:").pack(side="left")
        self.allowlist_var = tk.StringVar(value=str(allowlist_path))
        ttk.Entry(top, textvariable=self.allowlist_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="浏览...", command=self.browse_allowlist).pack(side="left")
        ttk.Button(top, text="刷新", command=self.refresh).pack(side="left", padx=4)
        ttk.Button(top, text="打开目录", command=self.open_allowlist_dir).pack(side="left")

        row = ttk.Frame(root, padding=(8, 0))
        row.pack(fill="x")
        ttk.Label(row, text="Builder:").pack(side="left")
        self.builder_var = tk.StringVar()
        self.builder_combo = ttk.Combobox(row, textvariable=self.builder_var, width=32, state="readonly")
        self.builder_combo.pack(side="left", padx=6)
        self.builder_combo.bind("<<ComboboxSelected>>", lambda _event: self.on_select())
        ttk.Button(row, text="检查状态", command=self.check_current, style="Secondary.TButton").pack(side="left", padx=4)

        status = ttk.LabelFrame(root, text="当前批准状态", padding=8)
        status.pack(fill="x", padx=8, pady=6)
        self.status_var = tk.StringVar(value="（未加载）")
        self.status_label = ttk.Label(status, textvariable=self.status_var, foreground="#555")
        self.status_label.pack(anchor="w")
        self.detail = tk.Text(
            status, height=4, wrap="word", state="disabled", bg="#FFFFFF",
            fg=TEXT, relief="flat", font=("Consolas", 9), padx=10, pady=8,
        )
        self.detail.pack(fill="x", pady=(6, 0))
        self.optimization_var = tk.StringVar(value="最近优化/建模运行：暂无")
        ttk.Label(status, textvariable=self.optimization_var, style="Muted.TLabel").pack(
            anchor="w", pady=(6, 0)
        )

        candidate = ttk.LabelFrame(root, text="候选新版本（要审核批准的文件）", padding=8)
        candidate.pack(fill="x", padx=8, pady=6)
        path_row = ttk.Frame(candidate)
        path_row.pack(fill="x")
        ttk.Label(path_row, text="文件:").pack(side="left")
        self.candidate_var = tk.StringVar()
        self.candidate_var.trace_add("write", lambda *_: self._update_candidate_status())
        self.candidate_entry = ttk.Entry(path_row, textvariable=self.candidate_var)
        self.candidate_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.candidate_entry.bind("<FocusOut>", lambda _event: self._save_ui_state())
        ttk.Button(path_row, text="浏览...", command=self.browse_candidate).pack(side="left")
        ttk.Button(path_row, text="打开所在文件夹", command=self.open_candidate_dir).pack(side="left", padx=4)
        self.full_path_var = tk.StringVar(value="完整路径：")
        self.full_path_label = ttk.Label(
            candidate, textvariable=self.full_path_var, style="Muted.TLabel", wraplength=1020
        )
        self.full_path_label.pack(fill="x", pady=(6, 0))
        candidate.bind(
            "<Configure>",
            lambda event: self.full_path_label.configure(wraplength=max(200, event.width - 40)),
        )
        self.candidate_status_var = tk.StringVar(value="（未选择候选文件）")
        ttk.Label(candidate, textvariable=self.candidate_status_var, foreground="#555").pack(
            fill="x", pady=(6, 0)
        )
        self.request_info_var = tk.StringVar(value="请求来源：（未选择候选）")
        ttk.Label(candidate, textvariable=self.request_info_var, style="Muted.TLabel", wraplength=1020).pack(
            fill="x"
        )
        action_row = ttk.Frame(candidate)
        action_row.pack(fill="x", pady=(6, 0))
        ttk.Button(action_row, text="检查候选版本", command=self.check_candidate, style="Accent.TButton").pack(side="left")
        self.full_diff_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(action_row, text="显示完整差异（可选）", variable=self.full_diff_var).pack(
            side="left", padx=8
        )
        ttk.Button(action_row, text="编辑来源信息", command=self.edit_source).pack(side="left")

        actions = ttk.Frame(root, padding=(8, 0))
        actions.pack(fill="x")
        ttk.Button(actions, text="批准并更新 allowlist", command=self.apply_approval, style="Accent.TButton").pack(side="left")
        self.pending_button = ttk.Button(
            actions,
            text="待审批请求 (0)",
            command=self.show_pending_requests,
            style="Secondary.TButton",
        )
        self.pending_button.pack(side="left", padx=6)
        self.optimization_button = ttk.Button(
            actions,
            text="优化运行 (0)",
            command=self.show_optimization_runs,
            style="Secondary.TButton",
        )
        self.optimization_button.pack(side="left", padx=6)
        ttk.Button(actions, text="编辑参数（表单）", command=self.edit_schema).pack(side="left", padx=6)
        ttk.Button(actions, text="打开 approved 目录", command=self.open_approved_dir).pack(side="left", padx=6)
        ttk.Checkbutton(
            actions,
            text="桌面通知",
            variable=self.notifications_var,
            command=self._save_ui_state,
        ).pack(side="left", padx=(12, 2))
        ttk.Button(actions, text="测试通知", command=self.test_notification).pack(side="left", padx=4)
        ttk.Button(actions, text="退出", command=self._on_close).pack(side="right")

        logbox = ttk.LabelFrame(root, text="检查摘要 / 日志", padding=8)
        logbox.pack(fill="both", expand=True, padx=8, pady=6)
        self.log = scrolledtext.ScrolledText(
            logbox, wrap="word", state="disabled", bg="#0B1220", fg="#E2E8F0",
            insertbackground="#E2E8F0", relief="flat", font=("Consolas", 9),
            padx=10, pady=8,
        )
        self.log.pack(fill="both", expand=True)
        self.refresh()
        self._restore_ui_state()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.watcher = ApprovalWatcher(
            lambda: self.allowlist_path,
            lambda: self.current_id,
            lambda: self._watch_candidate,
            lambda: bool(self.notifications_var.get()),
            self._notify_from_watcher,
            lambda text: self.root.after(0, lambda: self._log(text)),
            requests_getter=approval_pending_dir,
        )
        self.watcher.start()
        self.root.after(1000, self._refresh_pending_requests)
        self.root.after(300, self._tick_optimization)

    # ---- helpers -----------------------------------------------------
    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def _entry(self) -> dict | None:
        if not self.current_id:
            return None
        entry = self.data.get("builders", {}).get(self.current_id)
        return entry if isinstance(entry, dict) else None

    def _candidate(self) -> Path | None:
        text = self.candidate_var.get().strip()
        return Path(text).expanduser() if text else None

    # ---- remembered UI state ----------------------------------------
    def _restore_ui_state(self) -> None:
        """Restore the last candidate and builder used in this project."""
        last_builder = str(self.ui_config.get("last_builder_id") or "").strip()
        values = list(self.builder_combo["values"])
        if last_builder and last_builder in values:
            self.builder_var.set(last_builder)
            self.on_select()
        last_candidate = str(self.ui_config.get("last_candidate") or "").strip()
        if last_candidate and Path(last_candidate).is_file():
            self.candidate_var.set(last_candidate)

    def _save_ui_state(self) -> None:
        candidate = self._candidate()
        data = dict(self.ui_config)
        if candidate is not None and candidate.is_file():
            data["last_candidate"] = str(candidate)
        if self.current_id:
            data["last_builder_id"] = self.current_id
        data["notifications"] = bool(self.notifications_var.get())
        if data != self.ui_config:
            self.ui_config = data
            save_ui_config(self.ui_config_path, data)

    # ---- notifications / lifecycle ----------------------------------
    def test_notification(self) -> None:
        ok = windows_toast(
            "AEDT Builder Approver",
            "通知已启用：候选脚本更新或参数表缺项时，会在这里提醒你。",
        )
        self._log("[notify] test notification sent" if ok else "[notify] toast helper unavailable")

    def _notify_from_watcher(self, title: str, message: str) -> None:
        ok = windows_toast(title, message)

        def update_ui() -> None:
            self._log("[notify] %s%s" % (title, "" if ok else "（toast 不可用，使用提示音）"))
            try:
                self.root.bell()
            except Exception:  # noqa: BLE001
                pass

        try:
            self.root.after(0, update_ui)
        except Exception:  # noqa: BLE001 - window may already be closing
            pass

    # ---- approval request inbox --------------------------------------
    def _refresh_pending_requests(self) -> None:
        try:
            self.pending_requests = list_pending_requests()
        except Exception:  # noqa: BLE001
            self.pending_requests = []
        try:
            self.pending_button.configure(text="待审批请求 (%d)" % len(self.pending_requests))
            self.root.after(4000, self._refresh_pending_requests)
        except tk.TclError:
            pass

    def _canonical_candidate(self) -> str:
        candidate = self._candidate()
        return str(candidate.resolve()) if candidate is not None else ""

    def _complete_matching_request(self, *, new_hash: str = "", note: str = "") -> None:
        """Mark the active/matching request approved after a successful save."""
        request = self._active_request
        if request is None:
            canonical = self._canonical_candidate()
            for row in list_pending_requests():
                if row.get("builder_id") != self.current_id:
                    continue
                row_candidate = str(Path(str(row.get("candidate") or "")).expanduser().resolve())
                if row_candidate == canonical and (
                    not new_hash or str(row.get("candidate_sha256") or "").lower() == new_hash
                ):
                    request = row
                    break
        if request is None:
            return
        try:
            decide_approval_request(
                str(request["request_id"]),
                "approved",
                decided_by=getpass.getuser(),
                note=note or "approved via GUI",
            )
            self._log("[request] approved %s" % request["request_id"][:12])
        except Exception as exc:  # noqa: BLE001
            self._log("[request] failed to update status: %r" % (exc,))
        self._active_request = None
        self._refresh_pending_requests()

    def show_pending_requests(self) -> None:
        self._refresh_pending_requests()
        dialog = tk.Toplevel(self.root)
        dialog.title("待审批请求")
        dialog.geometry("980x480")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text="这些请求由守卫的 request_builder_approval 工具生成；选择一项载入候选文件。",
            style="Muted.TLabel",
            padding=10,
        ).pack(fill="x")

        columns = ("time", "builder", "type", "candidate", "reason", "source")
        tree = ttk.Treeview(dialog, columns=columns, show="headings", selectmode="browse")
        for key, title, width in (
            ("time", "请求时间", 150),
            ("builder", "Builder", 100),
            ("type", "类型", 100),
            ("candidate", "候选文件", 280),
            ("reason", "原因", 320),
            ("source", "来源", 160),
        ):
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor="w", stretch=key in {"candidate", "reason"})
        tree.pack(fill="both", expand=True, padx=10)

        def refresh() -> None:
            tree.delete(*tree.get_children())
            self._refresh_pending_requests()
            for row in self.pending_requests:
                source = " / ".join(
                    part for part in (
                        str(row.get("platform") or ""),
                        str(row.get("session_title") or ""),
                    ) if part
                )
                tree.insert(
                    "",
                    "end",
                    iid=str(row.get("request_id")),
                    values=(
                        str(row.get("requested_at") or ""),
                        str(row.get("builder_id") or ""),
                        str(row.get("request_type") or ""),
                        str(row.get("candidate") or ""),
                        str(row.get("reason") or ""),
                        source,
                    ),
                )
        refresh()

        def load_selected() -> None:
            selection = tree.selection()
            if not selection:
                messagebox.showinfo("先选择请求", "请先选择一个待审批请求。", parent=dialog)
                return
            request_id = selection[0]
            request = next((r for r in self.pending_requests if str(r.get("request_id")) == request_id), None)
            if request is None:
                return
            builder = str(request.get("builder_id") or "")
            if builder in list(self.builder_combo["values"]):
                self.builder_var.set(builder)
                self.current_id = builder
                self.check_current()
            candidate = str(request.get("candidate") or "")
            if candidate:
                self.candidate_var.set(candidate)
            self._active_request = request
            self._log(
                "[request] loaded %s (%s, %s)"
                % (request_id[:12], builder, candidate)
            )
            dialog.destroy()
            if str(request.get("request_type")) == "schema_only":
                self.root.after(100, self.edit_schema)

        buttons = ttk.Frame(dialog, padding=10)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="载入选中请求", command=load_selected, style="Accent.TButton").pack(side="left")
        ttk.Button(buttons, text="刷新", command=refresh).pack(side="left", padx=6)
        ttk.Button(buttons, text="关闭", command=dialog.destroy).pack(side="right")
        tree.bind("<Double-1>", lambda _event: load_selected())

    # ---- optimisation provenance ------------------------------------
    def _tick_optimization(self) -> None:
        self._refresh_optimization_runs()
        try:
            self.root.after(5000, self._tick_optimization)
        except tk.TclError:
            pass

    def _refresh_optimization_runs(self) -> None:
        try:
            self.optimization_rows = read_recent(100)
        except Exception:  # noqa: BLE001
            self.optimization_rows = []
        aster = sum(1 for row in self.optimization_rows if is_optimizer_run(row))
        manual = len(self.optimization_rows) - aster
        try:
            self.optimization_button.configure(text="优化运行 (%d)" % len(self.optimization_rows))
            latest = self.optimization_rows[0] if self.optimization_rows else None
            if latest is None:
                self.optimization_var.set("最近优化/建模运行：暂无")
            else:
                source = "aster/优化系统" if is_optimizer_run(latest) else "manual/手工"
                campaign = str(latest.get("campaign_id") or "—")
                status = str(latest.get("status") or "")
                self.optimization_var.set(
                    "最近优化/建模运行：%s    来源计数：aster %d / manual %d    campaign：%s    %s"
                    % (source, aster, manual, campaign, status)
                )
        except tk.TclError:
            pass

    def show_optimization_runs(self) -> None:
        self._refresh_optimization_runs()
        dialog = tk.Toplevel(self.root)
        dialog.title("优化运行来源")
        dialog.geometry("1120x560")
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(
            dialog,
            text=(
                "这里显示 guard 记录的真实 builder 运行：aster/优化系统 会带 campaign 与完成门槛；"
                "manual 表示手工/诊断运行，不计入优化完成度。"
            ),
            style="Muted.TLabel",
            padding=10,
        ).pack(fill="x")
        columns = ("time", "source", "campaign", "builder", "candidate", "status", "gate")
        tree = ttk.Treeview(dialog, columns=columns, show="headings", selectmode="browse")
        for key, title, width in (
            ("time", "时间", 155),
            ("source", "来源", 110),
            ("campaign", "Campaign", 220),
            ("builder", "Builder", 100),
            ("candidate", "候选/运行", 250),
            ("status", "状态", 110),
            ("gate", "完成门槛", 110),
        ):
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor="w", stretch=key in {"campaign", "candidate"})
        tree.pack(fill="both", expand=True, padx=10)
        row_by_iid: dict[str, dict] = {}

        def refresh() -> None:
            tree.delete(*tree.get_children())
            self._refresh_optimization_runs()
            for index, row in enumerate(self.optimization_rows):
                iid = "%s:%d" % (row.get("at_utc", "run"), index)
                row_by_iid[iid] = row
                aster = is_optimizer_run(row)
                source = "aster" if aster else "manual"
                campaign_id = str(row.get("campaign_id") or "")
                if aster and row.get("campaign_dir"):
                    gate = gate_for_campaign(str(row["campaign_dir"]))
                    gate_text = "PASS" if gate.get("passed") else "未完成"
                else:
                    gate_text = "—"
                tree.insert("", "end", iid=iid, values=(
                    str(row.get("at_utc") or ""),
                    source,
                    campaign_id or "—",
                    str(row.get("builder_id") or ""),
                    str(row.get("candidate_id") or row.get("run_id") or ""),
                    str(row.get("status") or ""),
                    gate_text,
                ))
            count = len(self.optimization_rows)
            aster_count = sum(1 for row in self.optimization_rows if is_optimizer_run(row))
            dialog.title("优化运行来源 — 共 %d 次，aster %d / manual %d" % (count, aster_count, count - aster_count))

        def open_selected() -> None:
            selection = tree.selection()
            if not selection:
                return
            row = row_by_iid.get(selection[0], {})
            target = row.get("campaign_dir") or row.get("outdir") or ""
            if target and Path(target).is_dir():
                os.startfile(target)  # noqa: S606

        refresh()
        buttons = ttk.Frame(dialog, padding=10)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="刷新", command=refresh).pack(side="left")
        ttk.Button(buttons, text="打开选中 campaign/outdir", command=open_selected).pack(side="left", padx=6)
        ttk.Button(buttons, text="关闭", command=dialog.destroy).pack(side="right")
        tree.bind("<Double-1>", lambda _event: open_selected())

    def _on_close(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()
        self.root.destroy()

    # ---- chrome ------------------------------------------------------
    def _build_header(self, root: tk.Tk) -> None:
        header = tk.Frame(root, bg=NAVY, height=96)
        header.pack(fill="x")
        header.pack_propagate(False)
        self._logo_img = None
        logo_path = asset_path("aedt_builder_approver_128.png")
        if logo_path.is_file():
            try:
                self._logo_img = tk.PhotoImage(file=str(logo_path))
                tk.Label(header, image=self._logo_img, bg=NAVY).pack(
                    side="left", padx=(22, 14), pady=14
                )
            except Exception:  # noqa: BLE001
                self._logo_img = None
        text_frame = tk.Frame(header, bg=NAVY)
        text_frame.pack(side="left", fill="y", pady=(17, 0))
        tk.Label(
            text_frame, text="AEDT Builder Approver", bg=NAVY, fg="#F8FAFC",
            font=("Segoe UI Semibold", 20),
        ).pack(anchor="w")
        tk.Label(
            text_frame,
            text="Operator console · review builder capabilities, not every run",
            bg=NAVY, fg="#94A3B8", font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(3, 0))
        tk.Label(
            header, text="LOCAL  ·  OPERATOR", bg="#132B3A", fg="#5EEAD4",
            font=("Segoe UI Semibold", 9), padx=10, pady=5,
        ).pack(side="right", padx=22)
        icon_path = asset_path("aedt_builder_approver.ico")
        if icon_path.is_file():
            try:
                root.iconbitmap(str(icon_path))
            except Exception:  # noqa: BLE001
                pass

    # ---- paths -------------------------------------------------------
    def browse_allowlist(self) -> None:
        path = filedialog.askopenfilename(title="选择 builder allowlist", filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if path:
            self.allowlist_var.set(path)
            self.allowlist_path = Path(path)
            self.refresh()

    def browse_candidate(self) -> None:
        last = str(self.ui_config.get("last_candidate") or "").strip()
        initialdir = str(Path(last).parent) if last else None
        initialfile = Path(last).name if last else None
        path = filedialog.askopenfilename(
            title="选择候选 builder 脚本",
            filetypes=[("Python", "*.py"), ("All", "*.*")],
            initialdir=initialdir,
            initialfile=initialfile,
        )
        if path:
            self.candidate_var.set(path)
            self._save_ui_state()

    def open_candidate_dir(self) -> None:
        candidate = self._candidate()
        if candidate and candidate.parent.is_dir():
            os.startfile(str(candidate.parent))  # noqa: S606

    def open_allowlist_dir(self) -> None:
        if self.allowlist_path.parent.is_dir():
            os.startfile(str(self.allowlist_path.parent))  # noqa: S606

    def open_approved_dir(self) -> None:
        self.approved_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.approved_dir))  # noqa: S606

    # ---- state -------------------------------------------------------
    def refresh(self) -> None:
        try:
            self.allowlist_path = Path(self.allowlist_var.get()).expanduser()
            self.data = load_allowlist(self.allowlist_path)
            ids = sorted(self.data.get("builders", {}))
            self.builder_combo["values"] = ids
            if ids:
                if self.current_id not in ids:
                    self.current_id = ids[0]
                self.builder_var.set(self.current_id)
                self.on_select()
            else:
                self.status_var.set("allowlist 中没有 builder")
        except Exception as exc:  # noqa: BLE001
            self.status_var.set("无法读取 allowlist: %s" % exc)
            self._log("错误: %r" % (exc,))

    def on_select(self) -> None:
        self.current_id = self.builder_var.get() or None
        self.check_current()
        self._update_candidate_status()

    def check_current(self) -> None:
        entry = self._entry()
        if not entry:
            self.status_var.set("未选择 builder")
            return
        try:
            ok, script, expected, actual = check_builder(entry)
            self.status_var.set("✅ 已批准副本一致" if ok else "❌ 已批准副本与 allowlist 不一致")
            self.status_label.configure(foreground="#1b7f3a" if ok else "#b00020")
            approval = entry.get("approval") if isinstance(entry, dict) else None
            approval_line = ""
            if isinstance(approval, dict):
                approval_line = (
                    "\n上次批准: %s by %s\n请求时间: %s\n请求来源: %s"
                    % (
                        approval.get("approved_at"),
                        approval.get("approved_by"),
                        approval.get("requested_at"),
                        approval.get("requested_source"),
                    )
                )
            params = entry.get("params") if isinstance(entry.get("params"), dict) else {}
            design_state = "已允许" if isinstance(params.get("design"), dict) else "未允许"
            self._set_detail(
                "script: %s\nallowlist sha256: %s\nactual   sha256: %s\n参数表: %d 项；design: %s%s"
                % (script, expected, actual, len(params), design_state, approval_line)
            )
            self._log("[check] %s: %s" % (self.current_id, "OK" if ok else "MISMATCH"))
        except Exception as exc:  # noqa: BLE001
            self.status_var.set("检查失败: %s" % exc)
            self._log("错误: %r" % (exc,))

    def _update_candidate_status(self) -> None:
        entry = self._entry()
        candidate = self._candidate()
        if not entry or candidate is None or not candidate.is_file():
            self.candidate_status_var.set("（未选择有效的候选文件）")
            self.request_info_var.set("请求来源：（未选择候选）")
            self.full_path_var.set("完整路径：")
            self._watch_candidate = ""
            self.request_meta = None
            self.request_meta_path = None
            self.request_meta_stale = False
            return
        self.full_path_var.set("完整路径：%s" % candidate)
        self._watch_candidate = str(candidate)
        try:
            current_hash = str(entry.get("sha256", "")).lower()
            candidate_hash = sha256(candidate)
            if candidate_hash == current_hash:
                self.candidate_status_var.set(
                    "候选与当前已批准版本一致（sha256 %s）" % candidate_hash[:16]
                )
            else:
                self.candidate_status_var.set(
                    "候选已变化：当前 %s → 候选 %s" % (current_hash[:16], candidate_hash[:16])
                )
            meta, path, stale = load_request_metadata(candidate)
            self.request_meta, self.request_meta_path = meta, path
            self.request_meta_stale = stale
            source = format_source(meta)
            line = "请求来源：%s    请求时间：%s" % (source, request_time(meta, candidate))
            reason = str(meta.get("reason") or "") if meta else ""
            if reason:
                line += "\n请求说明：%s" % reason
            if stale:
                line += "\n⚠ 来源元数据里的 candidate_sha256 与当前候选不一致，请确认/重写来源信息"
            self.request_info_var.set(line)
        except Exception as exc:  # noqa: BLE001
            self.candidate_status_var.set("无法读取候选文件: %s" % exc)
            self.request_info_var.set("请求来源：（无法读取候选）")

    # ---- metadata editor --------------------------------------------
    def edit_source(self) -> None:
        candidate = self._candidate()
        if candidate is None or not candidate.is_file():
            messagebox.showwarning("缺少候选文件", "请先选择候选脚本文件。")
            return
        meta = dict(self.request_meta or {})
        meta.setdefault("platform", "DeepSeek Harness")
        meta.setdefault("session_id", "")
        meta.setdefault("session_title", "")
        meta.setdefault("reason", "")
        meta.setdefault("requested_at", request_time(None, candidate))

        dialog = tk.Toplevel(self.root)
        dialog.title("编辑请求来源 — %s" % candidate.name)
        dialog.geometry("760x420")
        dialog.transient(self.root)
        fields = [
            ("平台（如 DeepSeek Harness / Codex）", "platform"),
            ("会话 ID", "session_id"),
            ("会话标题", "session_title"),
            ("请求时间", "requested_at"),
            ("请求说明/理由", "reason"),
        ]
        vars_: dict[str, tk.StringVar] = {}
        for index, (label, key) in enumerate(fields):
            ttk.Label(dialog, text=label).grid(row=index, column=0, sticky="w", padx=8, pady=4)
            var = tk.StringVar(value=str(meta.get(key, "")))
            vars_[key] = var
            if key == "reason":
                ttk.Entry(dialog, textvariable=var).grid(row=index, column=1, sticky="ew", padx=8)
            else:
                ttk.Entry(dialog, textvariable=var).grid(row=index, column=1, sticky="ew", padx=8)
        dialog.columnconfigure(1, weight=1)

        def save() -> None:
            record = {
                "builder_id": self.current_id,
                "platform": vars_["platform"].get().strip(),
                "session_id": vars_["session_id"].get().strip(),
                "session_title": vars_["session_title"].get().strip(),
                "requested_at": vars_["requested_at"].get().strip(),
                "reason": vars_["reason"].get().strip(),
                "candidate": str(candidate),
                "candidate_sha256": sha256(candidate),
            }
            target = candidate.with_name(candidate.stem + ".approval-request.json")
            target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._log("[source] wrote %s" % target)
            dialog.destroy()
            self._update_candidate_status()

        buttons = ttk.Frame(dialog, padding=8)
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="ew")
        ttk.Button(buttons, text="保存", command=save).pack(side="left")
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left", padx=6)

    # ---- schema editor ----------------------------------------------
    def edit_schema(self) -> None:
        entry = self._entry()
        if not entry:
            messagebox.showwarning("未选择 builder", "请先选择一个 builder。")
            return
        SchemaEditorDialog(
            self.root,
            self.current_id or "",
            entry.get("params") or {},
            self._candidate(),
            self._apply_schema,
        )

    def _apply_schema(self, params: dict) -> None:
        entry = self._entry()
        if not entry:
            raise ValueError("未选择 builder")
        if self.allowlist_path.is_file():
            backup = self.allowlist_path.with_name(
                self.allowlist_path.name
                + ".bak-"
                + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            )
            shutil.copy2(self.allowlist_path, backup)
        entry["params"] = params
        self.data["builders"][self.current_id] = entry
        save_allowlist(self.allowlist_path, self.data)
        self._log("[schema] %s params updated via form (%d params)" % (self.current_id, len(params)))
        if self._active_request and str(self._active_request.get("request_type")) == "schema_only":
            self._complete_matching_request(note="parameter schema updated via GUI")
        messagebox.showinfo("已保存", "参数表已更新并写入 allowlist。")
        self.refresh()

    # ---- candidate check -------------------------------------------
    def check_candidate(self) -> None:
        entry = self._entry()
        candidate = self._candidate()
        if not entry or candidate is None or not candidate.is_file():
            messagebox.showwarning("缺少文件", "请先选择 builder 和候选脚本文件。")
            return
        current = Path(str(entry.get("script", "")))
        before = current.read_text(encoding="utf-8", errors="replace").splitlines() if current.is_file() else []
        after = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        current_hash = str(entry.get("sha256", "")).lower()
        candidate_hash = sha256(candidate)
        matcher = difflib.SequenceMatcher(None, before, after)
        added = removed = blocks = 0
        changes: list[str] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            blocks += 1
            added += j2 - j1
            removed += i2 - i1
            for line in after[j1:j2][:3]:
                changes.append("+ " + line.strip()[:140])
            for line in before[i1:i2][:3]:
                changes.append("- " + line.strip()[:140])
        source = format_source(self.request_meta)
        self._log("\n=== 候选检查摘要（默认精简）===")
        self._log("builder: %s" % self.current_id)
        self._log("当前已批准: %s（%d 行, sha256 %s）" % (current, len(before), current_hash[:16]))
        self._log("候选新版本: %s（%d 行, sha256 %s）" % (candidate, len(after), candidate_hash[:16]))
        self._log("请求来源: %s" % source)
        self._log("请求时间: %s" % request_time(self.request_meta, candidate))
        self._log("变更: 新增 +%d 行 / 删除 -%d 行 / 变更块 %d 处" % (added, removed, blocks))
        if changes:
            self._log("\n关键变更行（最多前 12 条）:")
            for line in changes[:12]:
                self._log(line)
        if self.full_diff_var.get():
            diff = list(
                difflib.unified_diff(before, after, fromfile=str(current), tofile=str(candidate), lineterm="")
            )
            self._log("\n=== 完整差异（最多显示 2000 行）===")
            self._log("\n".join(diff[:2000]) if diff else "(无文本差异)")
        else:
            self._log("\n（默认只看摘要；需要全文请勾选“显示完整差异（可选）”后再点“检查候选版本”）")
        self._log(
            "\n检查清单：几何/边界/端口、是否删除工程、是否额外启动 AEDT、输出路径是否越界。\n"
            "确认无误后点击“批准并更新 allowlist”。"
        )

    # ---- approval ----------------------------------------------------
    def apply_approval(self) -> None:
        entry = self._entry()
        candidate = self._candidate()
        if not entry or candidate is None or not candidate.is_file():
            messagebox.showwarning("缺少文件", "请先选择 builder 和候选脚本文件。")
            return
        source = format_source(self.request_meta)
        requested_at = request_time(self.request_meta, candidate)
        reason = str(self.request_meta.get("reason") or "") if self.request_meta else ""
        old_hash = str(entry.get("sha256", "")).lower()
        new_hash = sha256(candidate)
        try:
            detected = detect_candidate_schema(candidate, entry.get("params") or {})
            suggestions = detect_suggestions(entry.get("params") or {}, detected)
            missing = list((suggestions.get("new") or {}).keys())
            missing += [
                "%s 的新可选值" % name for name in (suggestions.get("enum_additions") or {})
            ]
            if missing:
                if messagebox.askyesno(
                    "参数表还没更新",
                    "候选脚本里有参数表未声明的能力：\n\n  - %s\n\n是否先打开参数（表单）补充？"
                    % "\n  - ".join(missing),
                ):
                    self.edit_schema()
                    return
        except Exception as exc:  # noqa: BLE001 - detection is advisory only
            self._log("[schema-detect] skipped: %r" % (exc,))
        message = (
            "确认已完成人工 review，并批准新的 builder 版本？\n\n"
            "Builder: %s\n请求来源: %s\n请求时间: %s\n请求说明: %s\n当前 hash: %s\n候选 hash: %s\n参数数量: %d"
            % (
                self.current_id,
                source,
                requested_at,
                reason or "(未填写)",
                old_hash,
                new_hash,
                len(entry.get("params") or {}),
            )
        )
        if not messagebox.askyesno("确认批准", message):
            return
        try:
            self.approved_dir.mkdir(parents=True, exist_ok=True)
            target = self.approved_dir / (self.current_id + ".py")
            if self.allowlist_path.is_file():
                backup = self.allowlist_path.with_name(
                    self.allowlist_path.name
                    + ".bak-"
                    + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                )
                shutil.copy2(self.allowlist_path, backup)
            shutil.copy2(candidate, target)
            digest = sha256(target)
            approved_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            entry["script"] = str(target.resolve())
            entry["sha256"] = digest
            meta = dict(self.request_meta or {})
            meta.update(
                {
                    "candidate": str(candidate),
                    "candidate_sha256": new_hash,
                    "requested_at": requested_at,
                    "requested_source": source,
                    "approved_at": approved_at,
                    "approved_by": getpass.getuser(),
                    "previous_sha256": old_hash,
                    "approved_sha256": digest,
                }
            )
            entry["approval"] = meta
            self.data["builders"][self.current_id] = entry
            save_allowlist(self.allowlist_path, self.data)
            self._log(
                "[apply] %s\n  请求来源: %s\n  请求时间: %s\n  approved: %s\n  sha256: %s"
                % (self.current_id, source, requested_at, target.resolve(), digest)
            )
            self._complete_matching_request(new_hash=digest)
            messagebox.showinfo(
                "已批准",
                "已更新 allowlist 和 approved 副本。\n\n请求来源: %s\n请求时间: %s\n批准时间: %s\nsha256: %s"
                % (source, requested_at, approved_at, digest),
            )
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            self._log("apply 失败: %r" % (exc,))
            messagebox.showerror("批准失败", str(exc))


def self_test(allowlist: Path) -> int:
    try:
        data = load_allowlist(allowlist)
        results = {}
        ok_all = True
        for builder_id, entry in data.get("builders", {}).items():
            if not isinstance(entry, dict):
                ok_all = False
                results[builder_id] = {"ok": False, "error": "invalid entry"}
                continue
            ok, script, expected, actual = check_builder(entry)
            ok_all = ok_all and ok
            results[builder_id] = {
                "ok": ok,
                "script": script,
                "expected_sha256": expected,
                "actual_sha256": actual,
            }
        print(json.dumps({"allowlist": str(allowlist), "ok": ok_all, "builders": results}, indent=2))
        return 0 if ok_all else 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": repr(exc)}, indent=2))
        return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    parser.add_argument("--approved-dir", default=str(DEFAULT_APPROVED_DIR))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--report", default="")
    parser.add_argument("--test-notify", action="store_true")
    args = parser.parse_args()
    if args.test_notify:
        return 0 if windows_toast("AEDT Builder Approver", "通知测试成功") else 1
    if args.self_test:
        code = self_test(Path(args.allowlist))
        if args.report:
            try:
                allow = load_allowlist(Path(args.allowlist))
                payload = {
                    "allowlist": args.allowlist,
                    "builders": sorted(allow.get("builders", {})),
                    "exit": code,
                }
                Path(args.report).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        return code
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:  # noqa: BLE001
        pass
    ApproverApp(root, Path(args.allowlist), Path(args.approved_dir))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
