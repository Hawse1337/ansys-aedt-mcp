---
name: aedt-evidence-electromagnetics
description: Evidence-first Ansys Electronics Desktop and PyAEDT automation for Maxwell, HFSS, Q3D, Icepak, and related electromagnetic workflows. Use when the agent must check AEDT/PyAEDT, create or run a real AEDT design, verify the selected design and solution type, extract fields or matrices, export an .aedt project, or distinguish direct PyAEDT work from an AEDT MCP bridge.
---

# AEDT Evidence Electromagnetics

## 守卫强制规则（2026-11-20 起，最高优先级）

AEDT 操作**只能**通过 guard MCP 工具（本会话中名称为 `mcp__aedt__*`）。出现以下任一情况时必须
**停止并报告用户**，不得改用直连方式：

- 会话里看不到 `mcp__aedt__*` / `ansys-aedt-mcp` 工具；
- `connect_to_aedt` 返回 "unmanaged" / "not launched by this guard" 或类似拒绝；
- `guard_status` 出现 `unmanaged` 会话而任务要求使用它。

禁止事项（违反即视为绕过守卫）：

- 用 shell 启动 `ansysedt.exe`（无论是否带 `-grpcsrv`/`-ng`）；
- 直接 `import ansys.aedt.core` / PyAEDT 并用 `Hfss(...)`、`Desktop(...)` 建/开/改/存工程；
- 用 `Stop-Process`/`taskkill` 杀 AEDT 或删除 `.aedt.lock`；
- 自行设置 `AEDT_MCP_ALLOW_UNMANAGED`、`AEDT_ALLOW_UNGUARDED`、`AEDT_MCP_ALLOW_ARBITRARY_CODE`、`AEDT_GUARD_ALLOW_CREATE`、`AEDT_GUARD_ATTACH_OPEN` 或 `AEDT_GUARD_SEARCH_DIRS`。

启动/连接正确流程：

1. `mcp__aedt__check_aedt_installed`
2. `mcp__aedt__guard_status` — 记录 managed/unmanaged 状态
3. `mcp__aedt__launch_aedt(non_graphical=True, confirm_new_session=True)` — 只用 guard 启动
4. 用返回的 `port` 调 `mcp__aedt__check_aedt_connection` / `connect_to_aedt`
5. 收尾 `disconnect_from_aedt(close_desktop=true)` 或 `release_connection`

任何结构修改（几何、材料、馈电、setup）先向用户说明并取得明确同意，再执行；并在运行记录中保留
变更前后的结构和证据。

Use a user-owned AEDT installation through the guard. Never infer a successful electromagnetic
solve from `ansysedt.exe`, a project file, or a geometry screenshot.

## Start

> 本机（DSH + `ansys-aedt-mcp`）**没有** `ai-cae-toolbox` CLI，上游那几条命令执行会报
> “找不到命令”。用下面的「本机等价流程」代替；上游命令已注释，仅作对照。

<!-- 上游命令，本机不可用，勿执行：
ai-cae-toolbox context-scope --solver aedt
ai-cae-toolbox bridge-plan --solver aedt --objective "<objective>"
ai-cae-toolbox create-run --solver aedt --case <case-name> --objective "<objective>"
ai-cae-toolbox write-smoke-template --solver aedt --run-dir <run-dir>
-->

本机等价流程（MCP：`ansys-aedt-mcp`，63 个工具，部署在 `C:\ProgramData\ansys-aedt-mcp`）：

1. `check_aedt_installed` — 确认 AEDT 版本与安装目录
2. `check_aedt_status` / `list_aedt_sessions` — 看是否已有会话
3. 只通过 guard 启动/接入会话：
   - 启动：`launch_aedt(non_graphical=True, confirm_new_session=True)`，用返回的 port
   - 已由 guard 启动：`connect_to_aedt(port=<guard 返回的 port>)`
   - 用户自己的会话：先 `guard_status`；若显示 unmanaged，**停止**并让用户决定是否显式允许
4. `create_design` / `open_project` / 专用工具；`run_python_script`/`run_python_code` 默认被守卫拒绝（仅操作员可开启）
5. `validate_design` → `analyze_design` → `get_analysis_status` → `export_results`

工具名映射（上游文档里的名字在本机**不存在**，调用会失败）：

| 上游写法 | 本机实际工具 |
| --- | --- |
| `aedt_check_installation` | `check_aedt_installed` |
| `aedt_run_pyaedt_script_file` | `run_python_script`（文件）或 `run_python_code`（字符串） |
| `scan_run_evidence` | **未实现** → 用「证据清单」手工核对 |
| `generate_run_report` | **未实现** → 用「证据清单」手工写报告 |

## 本机运行要点（守卫模式）

- 只用 guard 的 `launch_aedt` 启动会话；它会选择可用的 gRPC 端口并登记为 managed，不要再手工 `ansysedt.exe -grpcsrv 2001`。
- 用户自己打开的 AEDT 是 **unmanaged**，`connect_to_aedt` 默认拒绝；需要用户显式决定，agent 不得自行放开。
- 求解状态看 guard 的 `get_analysis_status` / `wait_for_solve`；返回 idle 不等于收敛，仍需 `finalize_solve` 和证据核验。
- `export_results` 的 `output_path` 必填且只导出当前活动设计；不要写到 guard 状态目录。
- 收尾用 `disconnect_from_aedt(close_desktop=true)` 或 `release_connection`；不要用 `Stop-Process`/`taskkill` 清理 AEDT 或删除 `.aedt.lock`。
- `run_python_code` / `run_python_script` 默认被 guard 拒绝；需要专用工具，不要自行开启。

## Select the Correct Route

- Use the installed AEDT MCP (guard) for every operation. If the MCP is not
  available, STOP; do not fall back to direct PyAEDT or `ansysedt.exe`.
- Use an AEDT MCP only for capabilities it actually exposes.
- Do not build HFSS when the task requires Maxwell, or claim that an HFSS-focused
  MCP completed a Maxwell workflow.
- Record AEDT version, PyAEDT version, project, design name, design type, solution
  type, and whether a new or existing desktop session was used.

## Execute

Keep scripts in `scripts/`, source parameters in `inputs/`, project outputs in
`outputs/`, and field plots/tables in `exports/`. Make every geometry dimension,
material, excitation, boundary, setup, and sweep explicit.

For Maxwell matrix work, audit winding polarity and reciprocity. For HFSS, audit
ports, modes, radiation boundaries, frequency setup, adaptive convergence, and
S-parameter passivity. Read [references/acceptance-gates.md](references/acceptance-gates.md)
before interpreting solver output.

## 证据清单（替代未实现的 scan_run_evidence）

每次运行收尾逐项回答，缺项要写明「未检查」而不是留空：

- 环境：AEDT 版本、PyAEDT 版本、是否新开会话
- 工程：项目名、design 名、design type、solution type（与任务目标一致）
- 模型：单位、几何尺寸来源、材料、激励、边界、setup、扫频
- 求解：自适应收敛历史、求解频率范围、求解器日志无 fatal/许可/自适应失败
- 结果：S 参数幅度、**无源性**、**能量平衡**；Maxwell 另查 M12/M21 互易与自感为正
- 区分：**原生 AEDT 导出** vs **Python 派生图表**，后者必须显式标注

## 证据等级

| 等级 | 含义 |
| --- | --- |
| `functional-validation` | AEDT/PyAEDT 能启动、脚本能执行 |
| `engineering-draft` | 真实求解 + 原生工程、日志、数值数据、场图 |
| `report-grade` | engineering-draft + 收敛/敏感性检查 + 物理或实验验证 |

结论里必须写明本次达到哪一级，不要把「能跑通」当成「结果可用于工程判断」。

## Finish

Collect the AEDT project, solver/adaptive log, mesh statistics, numeric CSV/JSON,
field images, and a report. Clearly label any external circuit calculation or
Python-derived plot that is not a native AEDT result.

上游收尾命令（本机不可用，勿执行）：`ai-cae-toolbox scan-evidence <run-dir>`、
`ai-cae-toolbox generate-report <run-dir>`。本机改为：按「证据清单」逐项填写，
再把工程、日志、导出的 CSV/JSON、图片与报告放到同一个 `runs/<case>/` 目录下。
