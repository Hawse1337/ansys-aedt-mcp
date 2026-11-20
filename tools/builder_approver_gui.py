# -*- coding: utf-8 -*-
"""AEDT Builder Approver — small operator GUI for approved builders.

Double-click the built .exe to review a candidate builder, compare it with the current approved
copy, and update the allowlist hash/source path. Operator-only tool; no arbitrary code execution.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shutil
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

GUARD_DIR = Path(r"C:\ProgramData\ansys-aedt-mcp")
DEFAULT_ALLOWLIST = GUARD_DIR / "builders.local.json"
DEFAULT_APPROVED_DIR = GUARD_DIR / "approved"


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
    ok = actual == expected
    return ok, str(script), expected, actual


class ApproverApp:
    def __init__(self, root: tk.Tk, allowlist_path: Path, approved_dir: Path) -> None:
        self.root = root
        self.allowlist_path = allowlist_path
        self.approved_dir = approved_dir
        self.data: dict = {}
        self.current_id: str | None = None

        root.title("AEDT Builder Approver")
        root.geometry("980x720")
        root.minsize(820, 600)

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Allowlist:").pack(side="left")
        self.allowlist_var = tk.StringVar(value=str(allowlist_path))
        ttk.Entry(top, textvariable=self.allowlist_var, width=80).pack(side="left", padx=6)
        ttk.Button(top, text="浏览...", command=self.browse_allowlist).pack(side="left")
        ttk.Button(top, text="刷新", command=self.refresh).pack(side="left", padx=4)
        ttk.Button(top, text="打开目录", command=self.open_allowlist_dir).pack(side="left")

        row = ttk.Frame(root, padding=(8, 0))
        row.pack(fill="x")
        ttk.Label(row, text="Builder:").pack(side="left")
        self.builder_var = tk.StringVar()
        self.builder_combo = ttk.Combobox(row, textvariable=self.builder_var, width=30, state="readonly")
        self.builder_combo.pack(side="left", padx=6)
        self.builder_combo.bind("<<ComboboxSelected>>", lambda _event: self.on_select())
        ttk.Button(row, text="检查状态", command=self.check_current).pack(side="left", padx=4)

        status = ttk.LabelFrame(root, text="当前批准状态", padding=8)
        status.pack(fill="x", padx=8, pady=6)
        self.status_var = tk.StringVar(value="（未加载）")
        self.status_label = ttk.Label(status, textvariable=self.status_var, foreground="#555")
        self.status_label.pack(anchor="w")
        self.detail = tk.Text(status, height=4, wrap="word", state="disabled")
        self.detail.pack(fill="x", pady=(6, 0))

        candidate = ttk.LabelFrame(root, text="候选新版本（要批准的文件）", padding=8)
        candidate.pack(fill="x", padx=8, pady=6)
        self.candidate_var = tk.StringVar()
        ttk.Entry(candidate, textvariable=self.candidate_var).pack(side="left", fill="x", expand=True)
        ttk.Button(candidate, text="浏览...", command=self.browse_candidate).pack(side="left", padx=4)
        ttk.Button(candidate, text="查看差异", command=self.show_diff).pack(side="left")

        actions = ttk.Frame(root, padding=(8, 0))
        actions.pack(fill="x")
        ttk.Button(actions, text="批准并更新 allowlist", command=self.apply_approval).pack(side="left")
        ttk.Button(actions, text="打开 approved 目录", command=self.open_approved_dir).pack(side="left", padx=6)
        ttk.Button(actions, text="退出", command=root.destroy).pack(side="right")

        logbox = ttk.LabelFrame(root, text="日志 / Diff", padding=8)
        logbox.pack(fill="both", expand=True, padx=8, pady=6)
        self.log = scrolledtext.ScrolledText(logbox, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)

        self.refresh()

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

    # ---- actions -----------------------------------------------------
    def browse_allowlist(self) -> None:
        path = filedialog.askopenfilename(title="选择 build allowlist", filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if path:
            self.allowlist_var.set(path)
            self.allowlist_path = Path(path)
            self.refresh()

    def browse_candidate(self) -> None:
        path = filedialog.askopenfilename(title="选择候选 builder 脚本", filetypes=[("Python", "*.py"), ("All", "*.*")])
        if path:
            self.candidate_var.set(path)

    def open_allowlist_dir(self) -> None:
        target = self.allowlist_path.parent
        if target.is_dir():
            os.startfile(str(target))  # noqa: S606 - operator GUI action

    def open_approved_dir(self) -> None:
        self.approved_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.approved_dir))  # noqa: S606

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

    def check_current(self) -> None:
        entry = self._entry()
        if not entry:
            self.status_var.set("未选择 builder")
            return
        try:
            ok, script, expected, actual = check_builder(entry)
            self.status_var.set(
                ("✅ 已批准副本一致" if ok else "❌ 已批准副本与 allowlist 不一致")
            )
            self.status_label.configure(foreground="#1b7f3a" if ok else "#b00020")
            self._set_detail(
                "script: %s\nallowlist sha256: %s\nactual   sha256: %s" % (script, expected, actual)
            )
            self._log("[check] %s: %s" % (self.current_id, "OK" if ok else "MISMATCH"))
        except Exception as exc:  # noqa: BLE001
            self.status_var.set("检查失败: %s" % exc)
            self._log("错误: %r" % (exc,))

    def show_diff(self) -> None:
        entry = self._entry()
        candidate = Path(self.candidate_var.get()).expanduser()
        if not entry or not candidate.is_file():
            messagebox.showwarning("缺少文件", "请先选择 builder 和候选脚本文件。")
            return
        current = Path(str(entry.get("script", "")))
        before = current.read_text(encoding="utf-8", errors="replace").splitlines() if current.is_file() else []
        after = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        diff = list(
            difflib.unified_diff(before, after, fromfile=str(current), tofile=str(candidate), lineterm="")
        )
        self._log("\n=== DIFF ===")
        self._log("\n".join(diff[:4000]) if diff else "(无文本差异)")
        self._log(
            "\n检查清单：几何/边界/端口、是否删除工程、是否额外启动 AEDT、输出路径是否越界。\n"
            "确认无误后点击“批准并更新 allowlist”。"
        )

    def apply_approval(self) -> None:
        entry = self._entry()
        candidate = Path(self.candidate_var.get()).expanduser()
        if not entry or not candidate.is_file():
            messagebox.showwarning("缺少文件", "请先选择 builder 和候选脚本文件。")
            return
        if not messagebox.askyesno(
            "确认批准",
            "确认已人工 review 候选脚本，并把它设为新的已批准版本？\n\n%s" % candidate,
        ):
            return
        try:
            self.approved_dir.mkdir(parents=True, exist_ok=True)
            target = self.approved_dir / (self.current_id + ".py")
            if self.allowlist_path.is_file():
                backup = self.allowlist_path.with_name(
                    self.allowlist_path.name + ".bak-" + __import__("time").strftime("%Y%m%d-%H%M%S")
                )
                shutil.copy2(self.allowlist_path, backup)
            shutil.copy2(candidate, target)
            digest = sha256(target)
            entry["script"] = str(target.resolve())
            entry["sha256"] = digest
            self.data["builders"][self.current_id] = entry
            save_allowlist(self.allowlist_path, self.data)
            self._log(
                "[apply] %s\n  approved copy: %s\n  sha256: %s"
                % (self.current_id, target.resolve(), digest)
            )
            messagebox.showinfo("已批准", "已更新 allowlist 和 approved 副本。\n\n%s" % digest)
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
    args = parser.parse_args()
    if args.self_test:
        code = self_test(Path(args.allowlist))
        if args.report:
            # executable is windowed; write a report for automated verification
            try:
                allow = load_allowlist(Path(args.allowlist))
                payload = {"allowlist": args.allowlist, "builders": sorted(allow.get("builders", {})), "exit": code}
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
