# -*- coding: utf-8 -*-
"""A form-based parameter-schema editor for the Builder Approver GUI.

The operator edits a table, never raw JSON.  The guard still receives the same
strict schema; this module only removes the syntax-error class of failures.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, Mapping

from builder_schema import (
    PARAM_TYPES,
    apply_suggestions,
    detect_candidate_schema,
    detect_suggestions,
    summarize_spec,
    validate_schema,
)


class SchemaEditorDialog:
    def __init__(
        self,
        parent: tk.Misc,
        builder_id: str,
        params: Mapping[str, Any],
        candidate: Path | None,
        on_save: Callable[[dict[str, dict[str, Any]]], None],
    ) -> None:
        self.parent = parent
        self.builder_id = builder_id
        self.candidate = candidate
        self.on_save = on_save
        params = dict(params or {})
        design = params.pop("design", None)
        self.design_enabled = isinstance(design, dict)
        self.design_required = bool((design or {}).get("required", True))
        self.params: dict[str, dict[str, Any]] = {
            name: dict(spec) for name, spec in params.items() if isinstance(spec, dict)
        }

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("编辑参数（表单） — %s" % builder_id)
        self.dialog.geometry("960x680")
        self.dialog.minsize(800, 560)
        self.dialog.transient(parent)
        self.dialog.grab_set()

        header = ttk.Frame(self.dialog, padding=(12, 10, 12, 4))
        header.pack(fill="x")
        ttk.Label(
            header,
            text="参数表（不需要写 JSON；所有括号和逗号由程序生成）",
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w")
        ttk.Label(
            header,
            text="名称、类型、必填、范围和枚举值直接在这里填写。保存后立即写入 allowlist。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        design = ttk.LabelFrame(self.dialog, text="目标设计（design）", padding=10)
        design.pack(fill="x", padx=12, pady=6)
        self.design_enabled_var = tk.BooleanVar(value=self.design_enabled)
        self.design_required_var = tk.BooleanVar(value=self.design_required)
        ttk.Checkbutton(
            design,
            text="允许调用方传入 design（目标设计名）",
            variable=self.design_enabled_var,
            command=self._toggle_design,
        ).pack(anchor="w")
        self.design_required_check = ttk.Checkbutton(
            design,
            text="design 必填",
            variable=self.design_required_var,
        )
        self.design_required_check.pack(anchor="w", padx=(22, 0), pady=(2, 0))
        ttk.Label(
            design,
            text="这里只声明“允许 design 参数”，不固定具体设计；调用时例如 design=Trunc_10GHz。",
            style="Muted.TLabel",
        ).pack(anchor="w", padx=(22, 0), pady=(4, 0))

        table = ttk.LabelFrame(self.dialog, text="允许的参数", padding=8)
        table.pack(fill="both", expand=True, padx=12, pady=6)
        self.tree = ttk.Treeview(
            table,
            columns=("name", "type", "required", "rule"),
            show="headings",
            selectmode="browse",
            height=11,
        )
        for column, title, width in (
            ("name", "参数名", 220),
            ("type", "类型", 100),
            ("required", "必填", 70),
            ("rule", "范围 / 可选值", 430),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor="w", stretch=column == "rule")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _event: self._edit_selected())

        buttons = ttk.Frame(table)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="添加参数", command=self._add_param).pack(side="left")
        ttk.Button(buttons, text="编辑", command=self._edit_selected).pack(side="left", padx=4)
        ttk.Button(buttons, text="删除", command=self._delete_selected).pack(side="left")
        ttk.Button(buttons, text="从候选脚本检测", command=self._detect).pack(side="left", padx=(16, 4))

        self.status_var = tk.StringVar(value="")
        ttk.Label(self.dialog, textvariable=self.status_var, style="Muted.TLabel", padding=(12, 0)).pack(
            fill="x"
        )

        footer = ttk.Frame(self.dialog, padding=12)
        footer.pack(fill="x")
        ttk.Button(footer, text="保存参数表", command=self._save, style="Accent.TButton").pack(side="left")
        ttk.Button(footer, text="取消", command=self.dialog.destroy).pack(side="left", padx=6)
        ttk.Label(
            footer,
            text="保存只更新当前 allowlist；批准脚本 hash 仍走主界面的“批准并更新 allowlist”。",
            style="Muted.TLabel",
        ).pack(side="right")

        self._refresh_tree()
        self._toggle_design()

    def _toggle_design(self) -> None:
        self.design_required_check.configure(
            state="normal" if self.design_enabled_var.get() else "disabled"
        )

    def _refresh_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for name in sorted(self.params):
            spec = self.params[name]
            self.tree.insert(
                "",
                "end",
                iid=name,
                values=(name, spec.get("type", ""), "是" if spec.get("required") else "否", summarize_spec(spec)),
            )

    def _selected_name(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def _add_param(self) -> None:
        self._param_dialog(None)

    def _edit_selected(self) -> None:
        name = self._selected_name()
        if not name:
            messagebox.showinfo("先选择参数", "请在参数表中先选择一个参数。")
            return
        self._param_dialog(name)

    def _delete_selected(self) -> None:
        name = self._selected_name()
        if not name:
            return
        if not messagebox.askyesno("删除参数", "确定删除参数 %s？" % name):
            return
        self.params.pop(name, None)
        self._refresh_tree()

    def _param_dialog(self, existing_name: str | None) -> None:
        spec = dict(self.params.get(existing_name, {})) if existing_name else {}
        dialog = tk.Toplevel(self.dialog)
        dialog.title(("编辑参数 — %s" % existing_name) if existing_name else "添加参数")
        dialog.geometry("560x420")
        dialog.transient(self.dialog)
        dialog.grab_set()

        name_var = tk.StringVar(value=existing_name or "")
        type_var = tk.StringVar(value=str(spec.get("type") or "number"))
        required_var = tk.BooleanVar(value=bool(spec.get("required", False)))
        min_var = tk.StringVar(value="" if spec.get("min") is None else str(spec["min"]))
        max_var = tk.StringVar(value="" if spec.get("max") is None else str(spec["max"]))
        values_var = tk.StringVar(value=", ".join(str(v) for v in (spec.get("values") or [])))

        row = 0
        ttk.Label(dialog, text="参数名").grid(row=row, column=0, sticky="w", padx=10, pady=6)
        name_entry = ttk.Entry(dialog, textvariable=name_var)
        name_entry.grid(row=row, column=1, sticky="ew", padx=10)
        if existing_name:
            name_entry.configure(state="readonly")
        row += 1
        ttk.Label(dialog, text="类型").grid(row=row, column=0, sticky="w", padx=10, pady=6)
        type_combo = ttk.Combobox(dialog, textvariable=type_var, values=list(PARAM_TYPES), state="readonly")
        type_combo.grid(row=row, column=1, sticky="ew", padx=10)
        row += 1
        ttk.Checkbutton(dialog, text="必填（调用时必须提供）", variable=required_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=10, pady=4
        )
        row += 1

        number_frame = ttk.Frame(dialog)
        ttk.Label(number_frame, text="最小值").pack(side="left")
        ttk.Entry(number_frame, textvariable=min_var, width=14).pack(side="left", padx=(6, 16))
        ttk.Label(number_frame, text="最大值").pack(side="left")
        ttk.Entry(number_frame, textvariable=max_var, width=14).pack(side="left", padx=6)
        number_frame.grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=4)

        enum_frame = ttk.Frame(dialog)
        ttk.Label(enum_frame, text="可选值（用逗号分隔）").pack(anchor="w")
        ttk.Entry(enum_frame, textvariable=values_var).pack(fill="x", pady=(4, 0))
        enum_frame.grid(row=row, column=0, columnspan=2, sticky="ew", padx=10, pady=4)

        string_note = ttk.Label(dialog, text="string 类型无需范围；调用时传任意非空文本。", style="Muted.TLabel")
        string_note.grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=4)

        dialog.columnconfigure(1, weight=1)

        def refresh_type() -> None:
            kind = type_var.get()
            for widget in (number_frame, enum_frame, string_note):
                widget.grid_remove()
            if kind == "number":
                number_frame.grid()
            elif kind == "enum":
                enum_frame.grid()
            else:
                string_note.grid()

        type_combo.bind("<<ComboboxSelected>>", lambda _event: refresh_type())
        refresh_type()

        def save_param() -> None:
            name = name_var.get().strip()
            if existing_name:
                name = existing_name
            if not name:
                messagebox.showerror("保存失败", "参数名不能为空。", parent=dialog)
                return
            if name == "design":
                messagebox.showerror(
                    "保存失败",
                    "design 请用上方的“目标设计（design）”开关，不要在参数表里添加。",
                    parent=dialog,
                )
                return
            kind = type_var.get()
            candidate_spec: dict[str, Any] = {"type": kind, "required": required_var.get()}
            if kind == "number":
                for label, var, key in (("最小值", min_var, "min"), ("最大值", max_var, "max")):
                    text = var.get().strip()
                    if text:
                        try:
                            value = float(text)
                        except ValueError:
                            messagebox.showerror("保存失败", "%s必须是数字。" % label, parent=dialog)
                            return
                        candidate_spec[key] = value
            elif kind == "enum":
                values = [part.strip() for part in values_var.get().split(",") if part.strip()]
                if not values:
                    messagebox.showerror("保存失败", "枚举类型至少需要一个可选值。", parent=dialog)
                    return
                candidate_spec["values"] = values
            errors = validate_schema({name: candidate_spec})
            if errors:
                messagebox.showerror("保存失败", "\n".join(errors), parent=dialog)
                return
            self.params[name] = candidate_spec
            self._refresh_tree()
            dialog.destroy()

        footer = ttk.Frame(dialog, padding=10)
        footer.grid(row=row + 1, column=0, columnspan=2, sticky="ew")
        ttk.Button(footer, text="保存", command=save_param, style="Accent.TButton").pack(side="left")
        ttk.Button(footer, text="取消", command=dialog.destroy).pack(side="left", padx=6)

    def _detect(self) -> None:
        if self.candidate is None or not self.candidate.is_file():
            messagebox.showinfo("没有候选脚本", "请先在主界面选择候选 builder 脚本。")
            return
        try:
            detected = detect_candidate_schema(self.candidate, self.params)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("检测失败", "无法解析候选脚本：\n%s" % exc)
            return
        detected_design = isinstance(detected.get("design"), dict) or (
            isinstance(detected.get("design"), Mapping)
        )
        detected_params = {k: v for k, v in detected.items() if k != "design"}
        suggestions = detect_suggestions(self.params, detected_params)
        lines: list[str] = []
        for name, spec in suggestions["new"].items():
            lines.append("新增参数：%s（%s，%s）" % (name, spec.get("type"), summarize_spec(spec)))
        for name, values in suggestions["enum_additions"].items():
            lines.append("参数 %s 新增可选值：%s" % (name, ", ".join(str(v) for v in values)))
        if detected_design and not self.design_enabled_var.get():
            lines.append("候选脚本支持 design 参数（目标设计）")
        if not lines:
            messagebox.showinfo("检测完成", "没有发现需要补充的参数或枚举值。")
            return
        if not messagebox.askyesno("检测到候选脚本参数", "\n".join(lines) + "\n\n是否全部加入参数表？"):
            return
        self.params = apply_suggestions(self.params, detected_params)
        if detected_design:
            self.design_enabled_var.set(True)
            self.design_required_var.set(True)
        self._toggle_design()
        self._refresh_tree()
        self.status_var.set("已从候选脚本补全 %d 项；请检查后保存。" % len(lines))

    def _save(self) -> None:
        schema: dict[str, dict[str, Any]] = {}
        if self.design_enabled_var.get():
            spec: dict[str, Any] = {"type": "string"}
            if self.design_required_var.get():
                spec["required"] = True
            schema["design"] = spec
        for name, spec in self.params.items():
            schema[name] = dict(spec)
        errors = validate_schema(schema)
        if errors:
            self.status_var.set("；".join(errors[:3]))
            messagebox.showerror("参数表有错误", "\n".join(errors))
            return
        try:
            self.on_save(schema)
        except Exception as exc:  # noqa: BLE001
            self.status_var.set("保存失败：%s" % exc)
            messagebox.showerror("保存失败", str(exc))
            return
        self.dialog.destroy()
