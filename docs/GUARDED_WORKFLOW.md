# 受控工作流（Guarded Workflow, v1.7）

本文件说明 v1.6/v1.7 新增的“规格 + 评测 + 受控运行”层：它建立在 v1.5 的求解证据工具之上，
把“Agent 自由调用 AEDT”变成“Agent 只能在规格与状态机内提交候选”。

## 1. 目录布局（一个 campaign 一个目录）

```
<campaign_dir>/
  design_spec.json        # 当前规格（版本化、带审批历史的审计记录）
  campaign.sqlite         # runs / metrics / spec_versions
  runs/<run_id>/
    run.json              # 提交快照（项目/设计/setup/参数/切面/频率/规格版本）
    artifacts/
      result.s1p          # Touchstone（建议指定 sweep_name，取整个扫频）
      farfield/*.csv      # θ 切面与 φ 切面
```

建议环境变量（生产 `mcp_config.json` 或每次启动）：

```
AEDT_GUARD_SEARCH_DIRS=<campaign 的 projects 目录>   # 让工程守卫解析裸工程名
AEDT_CAMPAIGN_ROOT=<campaign 根目录>                # spec_* 工具的默认根
```

## 2. 工具

| 工具 | 作用 | 是否碰 AEDT |
|---|---|---|
| `spec_create` | 建立 v1 规格（指标/频段/评测参数/执行策略） | 否 |
| `spec_get` | 读当前规格 + 版本历史 | 否 |
| `spec_update_with_approval` | **必须**提供 reason + approver，版本 +1 并记录 diff | 否 |
| `campaign_context` | 给 Agent 的摘要：硬指标、软目标、最近 run、未满足约束、下一步提示 | 否 |
| `evaluate_exported_results` | 用已导出文件算指标，可写回某个 run 的 metrics | 否 |
| `submit_guarded_run` | 立即返回 run_id，后台线程执行完整状态机 | 是 |
| `get_run_status` | 读状态、阶段日志、指标（只有 COMPLETED 的 run 才有指标） | 否 |

## 3. 状态机

```
CREATED → PREPARED → SOLVING → SOLVED → EXPORTING → VALIDATING → COMPLETED
                                   ↘ RECOVERY_REQUIRED（求解超时 / 未保存 / finalize 失败）
                                   ↘ FAILED（preflight 失败 / 解数据缺失 / 导出或评测失败）
```

- `PREPARED`：`preflight_design` 通过（validate + 无限球 + 解数据探测）；
- `SOLVING`：提交成功（`started=true`），随后轮询 `analysis_status` 直到空闲或超时；
- `SOLVED`：空闲；**空闲不等于收敛**，因此仍需下一步；
- `RECOVERY_REQUIRED` 而不是 FAILED 的两种情况：求解超时（可能仍在跑）、项目未保存；
- 只有 `COMPLETED` 才写入 metrics 并允许 Agent 读取。

## 4. 实机验证（2026-11-09，AEDT 2026 R1，沙箱 2.5 GHz 偶极子）

| 项 | 值 |
|---|---|
| run_id | `20261109T114443414905` |
| 状态链 | PREPARED → SOLVING → SOLVED（119 次轮询，9m57s）→ EXPORTING → VALIDATING → COMPLETED |
| 指标 | `gain_h` 1.47 dBi（通过）、`ripple` 0.067 dB（通过）、`s11` **-5.15 dB（硬约束未通过）** |
| 结论 | `feasible=false`，`unmet=["s11"]`；`campaign_context` 正确给出“先满足硬约束”的提示 |
| 产物 | `result.s1p`（2.0–3.0 GHz 共 11 点扫频）+ θ 切面 CSV + φ 切面 CSV |

## 5. 强制层（P3，v1.8）

### 5.1 开启方式

| 环境变量 | 作用 |
|---|---|
| `AEDT_GUARD_ENFORCE=1` | 打开强制：所有求解入口必须带合法 `run_token` |
| `AEDT_CAMPAIGN_ROOT=<campaign 目录>` | 审计写入该目录（未给时从 token 路径推断） |
| `AEDT_ALLOW_UNGUARDED=1` | 逃生口：允许无 token 提交，但**记入审计**并计入 `unguarded_count` |

### 5.2 令牌

- `submit_guarded_run` 在提交前为本次 run 签发 `runs/<run_id>/run_token.json`；
- 令牌用 campaign 目录内的 `.guard_key` 做 HMAC-SHA256 签名，绑定 `project / design / setup`，带过期时间；
- 后台 runner 结束时自动撤销令牌。

### 5.3 拦截点

| 入口 | 行为 |
|---|---|
| `analyze_design`（含 `submit_guarded_run` 内部调用） | 无 token / token 不匹配 → `E_UNGUARDED_ANALYZE` |
| `start_analysis` | 同上 |
| `run_python_code` | 代码中出现 `analyze`（含 `odesign.Analyze`）且无合法 token → `E_UNGUARDED_ANALYZE`；其余代码不受影响 |

审计写入 `<campaign>/audit/analyze_audit.jsonl`，每条记录：时间、命令、project/design/setup、是否有 token、是否放行、原因、run_id。`campaign_context` 返回 `analysis_audit`：

```
{"audit_entries": n, "denied": n, "unguarded_count": n, "escape_hatch": n, "governed": n}
```

> 这是“防止误绕过”的执行纪律层，**不是安全沙箱**：伪造 token（能写 campaign 目录的人）或绕开 MCP 直接操作 AEDT 都不在防护范围内。

### 5.4 实机验收（2026-11-09，AEDT 2026 R1，`AEDT_GUARD_ENFORCE=1`）

| 场景 | 结果 |
|---|---|
| `run_python_code` 执行 `app.analyze_setup(...)`（无 token） | ✅ 被拒：`E_UNGUARDED_ANALYZE: missing run_token` |
| `start_analysis` 直接提交（无 token） | ✅ 被拒：同上 |
| `submit_guarded_run`（内部签发 token） | ✅ 通过，run `20261109T144213911670` 走完 SOLVING → **COMPLETED** |
| 指标 | `gain_h` 1.47 dBi（通过）、`s11` -5.15 dB（硬约束未通过） |
| 审计统计 | `audit_entries=7, denied=6, unguarded_count=6, governed=1`（6 次拒绝含前两次失败会话的尝试） |

实测中还确认了一个副作用：被拒绝的调用**不会**启动求解（`analyze_setup` 未被调用），也没有污染工程。

## 5A. 崩溃恢复（P4-1，v1.9）

求解很贵，进程崩溃不应该让求解白做。v1.9 增加 `resume_guarded_run`：

| 情形 | 行为 |
|---|---|
| 进程在 `SOLVING/SOLVED/EXPORTING` 时死亡 | 状态停在最后一步（非终态），重启后可恢复 |
| `resume_guarded_run(campaign_dir, run_id, pid/port)` | 从**保存校验 + 导出 + 评测**继续，**绝不重新求解** |
| 解数据缺失（工程没保存/被改） | 失败关闭（FAILED），不会伪造 COMPLETED |
| 已 COMPLETED/FAILED | 直接返回原状态，不重复执行 |

实机演练（AEDT 2026 R1，5 GHz 偶极子，5 分钟内两次）：

| 阶段 | 观察 |
|---|---|
| 提交后在第 4 步人为 `os._exit`（模拟宿主进程被杀） | 状态停在 `EXPORTING`，MCP server 进程消失，AEDT 仍在，产物只有 `result.s1p`（远场未导出） |
| `resume_guarded_run` | `resumed_from=EXPORTING`，随后 `VALIDATING → COMPLETED` |
| 恢复后阶段日志 | `PREPARED, SOLVING, SOLVED, EXPORTING, EXPORTING(resumed_from), VALIDATING, COMPLETED` |
| 指标 | 与崩溃前那次求解完全一致（gain 2.0658 dBi、s11 -7.6356 dB），**证明复用同一份解，没有重算** |

要点：状态日志在恢复时**保留原有历史**（只追加），所以"求解只发生一次"在日志里可核对。

## 5B. 缓存、归档与收敛门（P4-2/3/4，v1.10）

### 缓存命中
`submit_guarded_run` 先算 `cache_key = sha256(project/design/setup/spec_version/params/cuts/frequency/sweep)`；命中同一 `campaign` 下**已 COMPLETED** 的 run 时直接返回：

```json
{"run_id": "<原 run>", "status": "COMPLETED", "cached": true, "metrics": {...}}
```

不启动 worker、不求解、不导出。参数或规格版本变化即 miss。

### 归档与覆盖保护
- `archive_query(campaign_dir)` 返回 `best`、`pareto_front`、`ranked`；
- 目标函数默认“最大化 realized_gain_h”，可用 spec 的 `objectives` 声明多目标（如同时最小化 s11）；
- `commit_best` 在 COMPLETED 后自动执行：
  - 有可行最优时，**不可行候选不能覆盖**（`refused_infeasible_overwrite`）；
  - 更好的可行候选才替换（`not_better_than_best` 否则拒绝）；
  - 尚无可行解时记录“已知最优（不可行）”并标注 `feasible=false`。

### 收敛证据门
- `execution_policy.require_convergence`（默认 `true`）；
- COMPLETED 之前导出 `.conv` 并用 `solve_evidence.summarize_hfss_convergence` 判定；
- `assessment != "converged"` 且 `native_converged != true` → **FAILED（E_NOT_CONVERGED）**，不给指标；
- 注意：若工程里是**旧解**，此处会正确地拒绝（实测中正是这样挡住的）。

### 实机验证（AEDT 2026 R1，5 GHz 偶极子）

| 项 | 结果 |
|---|---|
| 干净重算一次（313 s） | COMPLETED，`convergence={"native_converged": true, "assessment": "converged", "target_delta_s": 0.1}` |
| 指标 | `gain_h` 1.85 dBi（过）、`s11` -6.59 dB（硬约束未过） |
| 相同参数再次提交 | **cached=true**，返回同一 run_id，耗时 **0.0 s** |
| `archive_query` | `best` 指向该 run（`feasible=false`，因无可行解）、`pareto_front=[]`、`ranked` 按目标排序 |
| 旧解/未收敛解 | 两次被 `E_NOT_CONVERGED` 拒绝，未产出指标 |

## 5C. 多频点远场与预算强制（P4-5/6，v1.11）

### 多频点远场

- 频点来源优先级：本次运行参数 `frequencies_ghz` → spec 的 `evaluation.frequencies_ghz` →
  `evaluation.frequency_sampling`（linear/log + points）→ 默认 `[low, 中心, high]`；
- 上限 `evaluation.max_frequency_points`（默认 9），超限报 `E_FREQ_POINT_BUDGET`；超出 spec 频段直接拒绝；
- 导出键形如 `theta_phi0@4.8GHz`，运行时按频点分组为 `artifacts.farfield["4.8"]`；
- 指标：每个频点都算增益/峰值角/纹波/副瓣，硬约束用**最差频点**判定，细节保留 `per_frequency`；
- 频点在扫频里没有场数据时 → `E_FF_FREQ_UNAVAILABLE`（提示开启 Save Fields 或只用自适应频点）；
- 频点列表进入 cache_key，换频点不会命中旧缓存。

### 预算强制

- `execution_policy.max_run_minutes` 现在真正生效：等待超时被 `min(timeout_s, 预算)` 截断；
- 超预算 → 调用 `abort_analysis`（`stop_simulations(clean_stop=True)`）→ 运行标记 **FAILED / BUDGET_EXCEEDED**，
  不会给出指标；
- 与"求解超时"区分：超时且预算未到 → `RECOVERY_REQUIRED`（可能还在跑）；预算到 → 主动停。

### 实机验证（AEDT 2026 R1）

| 项 | 结果 |
|---|---|
| 预算（0.05 min = 3 s） | 9.1 s 后 FAILED：`BUDGET_EXCEEDED: exceeded max_run_minutes=0.05 and was stopped`，abort 返回 `stopped: true` |
| 多频点（4.8/5.0/5.2 GHz，Discrete 扫频 save_fields） | **COMPLETED**（350.9 s），artifacts 里三个频点各一组 θ/φ CSV |
| 指标 | `gain_h` 最差频点 1.39 dBi（过）、`ripple` 0.18 dB（过）、`s11` -6.59 dB（硬约束未过） |
| 求解进程 | 求解期间 22 个 `hf3d` 进程池只有 1 个在算（实测 +3.83 s CPU/6 s），其余等待；求解结束全部自动退出 |
| 清理 | 沙箱 AEDT 已全部关闭，机器无残留求解进程 |

## 5D. 求解并行度（核数 / 任务数 / GPU）（v1.12）

控制面与频点一致，三层：

```json
"execution_policy": {
  "num_cores": 8, "num_tasks": 1, "num_gpus": 0,
  "max_total_cores": 32,
  "enforce_cpu_budget": false
}
```

| 层 | 写法 | 说明 |
|---|---|---|
| campaign 默认 | spec 的 `execution_policy` | 不写 = 不向 HFSS 传参（老行为不变） |
| 单次覆盖 | `submit_guarded_run(num_cores=16, num_tasks=2)` | 只影响这一次；`resume` 同样接受 |
| 边界 | `max_total_cores` / 逻辑核数 | 默认**收敛并警告**；`enforce_cpu_budget=true` → 报 `E_CORE_BUDGET` |

- `cores × tasks` 为总核数；`num_gpus=0` 不会发送（HFSS 只接受正整数或空）；
- 解析结果回显在 `submit_guarded_run` 与 `get_run_status` 的 `solver_settings`（requested/effective/warnings）；
- 求解并行度**不进入 cache_key**（结果与并行度无关，已用实测验证）；
- 没有 HPC 许可却设 `tasks>1` 时不会静默失败：HFSS 的报错原样带回。

实机对比（同一 5 GHz 三频点偶极子，完全相同设置）：

| 并行度 | 墙钟时间 | 指标 |
|---|---|---|
| 1 核 | 350.9 s | gain 1.3882630552402984 dBi / s11 -6.589 dB |
| 4 核 | **40.1 s** | gain 1.3882630552402984 dBi / s11 -6.589 dB（**逐位相同**） |

## 6. 已知限制（下一阶段处理）

1. **S11 带宽依赖导出对象**：不传 `sweep_name` 时 Touchstone 只含自适应解频点；要做真实带内判定必须传
   `sweep_name`（本次已传 `Sweep2to3`）。建议把“必须有扫频”加入 spec 的评测参数。
2. **单频点增益**：远场是单个频率的导出，`realized_gain_h` 用该频点的 S11 做失配修正；跨频带增益需要多频点导出。
3. **收敛证据尚未纳入判定**：`solve_evidence`（profile/convergence）已经能解析，但 `COMPLETED` 目前只看解数据存在性；
   下一步把“收敛且无 fatal”作为 COMPLETED 的附加条件。
4. **强制层未启用**：`run_python_code` 仍可直接调用 `Analyze`；campaign 锁目前只做建议性排他（P3 要在 worker 层校验 run_token）。
5. **无缓存/无 Pareto/无归档**（P4）。
