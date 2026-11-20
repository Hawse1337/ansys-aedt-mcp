# v1.5 deployment record（2026-11-09）

## 内容
- 基线：hardening kit **v1.4.0-rc1**（`install.py --install --pyproject --pyaedt`，安装后 146 项测试通过）
- 增量：**guarded-workflow v1.5**（向后兼容）
  - `analyze_design(blocking=true)`：提交后轮询 `analysis_status`，新增 `blocking_status/elapsed_s/poll_count` 等字段；默认路径返回键集合与 v1.4 一致
  - 新工具 8 个：`preflight_design`、`finalize_solve`、`export_farfield_report`、`wait_for_solve`、`verify_artifacts`、`clear_stale_lock`、`acquire_campaign_lock`、`release_campaign_lock`
  - analyze 新增证据字段 `ff_sphere_present`、`solution_data_present_before_submit`（`None` = 无法判断）
  - worker 退出 flush stdout/stderr
- 工具总数：**45 → 53**

## 验证
- 离线：`python -m unittest discover -s tests` → **275 项通过**（新增 29 项，含 blocking 三态、schema 兼容快照、锁解析、KeyError 健壮性）
- 实机（独立 AEDT + 沙箱偶极子工程，SAC 关闭）：build/preflight/solve/status/finalize/farfield/verify_artifacts **全部通过**；far-field θ 切面 92 行、θ=90° 水平面 182 行，`non_empty=true`
- 部署后再次实机复跑：通过（生产代码路径）

## 实机暴露并修复的问题
1. `InsertInfiniteSphereSetup` 需要**先有辐射边界（open region）**；缺失时 AEDT 报错并令同会话后续 gRPC 命令全部失败。工具错误信息已注明该前置条件。
2. Terminal/混合解型下遍历 `app.setups` 会抛 `KeyError("HFSS Terminal Network")` → 探测函数改为返回 `None`；`finalize_solve` 未显式传 `setups` 时给出可操作错误。
3. `clear_stale_lock` 改为解析 AEDT 锁的 `DesktopProcessID=NNN` 元数据（原“首个数字”会误把端口号/版本当 PID）。

## Git
- 分支：`v1.5-hardening`
- 提交：`v1.5: guarded-workflow extensions on hardening kit v1.4`
- 标签：`v1.5.0`；升级前基线标签：`v1.3-deployed-baseline`（指向升级前 HEAD）

## 回滚
```powershell
git -C C:\ProgramData\ansys-aedt-mcp checkout master         # 回到升级前分支
git -C C:\ProgramData\ansys-aedt-mcp reset --hard v1.3-deployed-baseline
```
本地文件备份：`D:\AgentAEDT_Collab\backups\prod-pre-v15-20261109-173531`
（另：`install.py --install` 自身在 `.aedt-hardening-backups/` 留有逐文件批次，已被 .gitignore 排除）

## 注意事项
- MCP 客户端配置（`mcp_config.json`）以 `cwd=C:\ProgramData\ansys-aedt-mcp` 启动 `mcp_server.py`；**重启 MCP 服务后**工具列表才会变成 53 个。
- 本机 venv 无 setuptools，无法 `pip install -e .` 重建 egg-info；worker 与 server 均以仓库目录为 cwd 启动，模块解析不依赖 egg-info。
- `install.py --check` 现在会报告与 kit v1.4 哈希的差异（属预期：v1.5 覆盖了 4 个文件）。
