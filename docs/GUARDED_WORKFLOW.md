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

## 5. 已知限制（下一阶段处理）

1. **S11 带宽依赖导出对象**：不传 `sweep_name` 时 Touchstone 只含自适应解频点；要做真实带内判定必须传
   `sweep_name`（本次已传 `Sweep2to3`）。建议把“必须有扫频”加入 spec 的评测参数。
2. **单频点增益**：远场是单个频率的导出，`realized_gain_h` 用该频点的 S11 做失配修正；跨频带增益需要多频点导出。
3. **收敛证据尚未纳入判定**：`solve_evidence`（profile/convergence）已经能解析，但 `COMPLETED` 目前只看解数据存在性；
   下一步把“收敛且无 fatal”作为 COMPLETED 的附加条件。
4. **强制层未启用**：`run_python_code` 仍可直接调用 `Analyze`；campaign 锁目前只做建议性排他（P3 要在 worker 层校验 run_token）。
5. **无缓存/无 Pareto/无归档**（P4）。
