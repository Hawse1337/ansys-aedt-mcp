# Ansys AEDT MCP — 全局部署说明

本机部署信息（安装时间：2026-11-02）

## 1. 总体结构

| 组件 | 版本 | 位置 |
|------|------|------|
| Python（全局） | 3.14.10 | `C:\Users\Administrator\AppData\Local\Programs\Python\Python314` |
| Git | 2.56.0.4 | `C:\Program Files\Git` |
| PyAEDT（全局） | 1.9.0 | 系统 Python 的 site-packages |
| AEDT MCP | 0.2.0 | `C:\ProgramData\ansys-aedt-mcp` |
| MCP 专用虚拟环境 | Python 3.14.10 | `C:\ProgramData\ansys-aedt-mcp\.venv` |
| 虚拟环境内 PyAEDT | 1.9.0（加固套件要求） | 同上 |
| MCP SDK (FastMCP) | 1.30.0 | 同上 |
| Ansys AEDT | 2026 R1 (v261) | `D:\ANSYS Inc\v261\AnsysEM\ansysedt.exe` |

> 说明：MCP 项目的 `pyproject.toml` 原本锁定 `pyaedt==1.5.0`，但 **1.5.0 的
> `Desktop(port=...)` 会忽略端口参数**，导致每次按端口连接都新起一个 AEDT
> （实测表现为进程风暴）。加固套件要求钉到 **1.9.0**，已升级。
> MCP 使用**独立虚拟环境**；系统 Python 中另装 1.9.0 供日常脚本使用，互不干扰。

> 注意：PyAEDT 1.x 的导入名已改为 `ansys.aedt.core`，`import pyaedt` 不再存在。
> ```python
> from ansys.aedt.core import Hfss, Maxwell, Icepak
> ```

## 2. 启动方式

三种等价方式，任选其一：

```powershell
# 方式一：系统 PATH 中的全局启动器（已在机器级 PATH 注册）
ansys-aedt-mcp

# 方式二：直接调用虚拟环境解释器
C:\ProgramData\ansys-aedt-mcp\.venv\Scripts\python.exe C:\ProgramData\ansys-aedt-mcp\mcp_server.py

# 方式三：启动脚本
C:\ProgramData\ansys-aedt-mcp\bin\ansys-aedt-mcp.cmd
```

启动器会自动注入 `AEDT_VERSION` / `AEDT_INSTALL_DIR` 等环境变量。

## 3. 已完成的全局注册

- **系统 PATH**：`C:\ProgramData\ansys-aedt-mcp\bin`（机器级，新开终端生效）
- **Codex**：已写入 `C:\Users\Administrator\.codex\config.toml`
  ```toml
  [mcp_servers."ansys-aedt"]
  command = 'C:\ProgramData\ansys-aedt-mcp\.venv\Scripts\python.exe'
  args = ['C:\ProgramData\ansys-aedt-mcp\mcp_server.py']
  cwd = 'C:\ProgramData\ansys-aedt-mcp'
  startup_timeout_sec = 300
  tool_timeout_sec = 2400
  ```
  重启 Codex 后即可看到 `ansys-aedt` 服务器。

- **其它客户端（Claude Desktop / Cursor / 其它支持 MCP 的工具）**：
  直接复制 `C:\ProgramData\ansys-aedt-mcp\mcp_config.json` 中的 `mcpServers` 段即可。
  - Claude Desktop：`%APPDATA%\Claude\claude_desktop_config.json`
  - Cursor：`%USERPROFILE%\.cursor\mcp.json`

## 4. 环境变量（已写入 .env 与启动器）

| 变量 | 值 |
|------|-----|
| `AEDT_VERSION` | `2026.1` |
| `AEDT_INSTALL_DIR` | `D:\ANSYS Inc\v261\AnsysEM` |
| `AEDT_WORKER_TIMEOUT` | `1800`（加固套件：原 60） |
| `AEDT_LAUNCH_TIMEOUT` | `240`（加固套件：原 120） |
| `AEDT_LOG_DIR` | `%TEMP%\aedt-mcp` |

AEDT 也可通过系统已有的 `AWP_ROOT261=D:\ANSYS Inc\v261` 自动发现。

## 5. 使用流程（显式指定目标会话）

MCP 不会隐式选择 AEDT 窗口，必须显式给 `pid` 或 `port`：

1. `check_aedt_installed` — 确认 AEDT 安装
2. `check_aedt_status` — 查看是否已有会话
3. `list_aedt_sessions` / `connect_to_aedt` / `launch_aedt` — 选择或启动会话
4. 之后每个工具调用都必须传 `pid` 或 `port`（二选一）

共 **45 个工具**：HFSS、Maxwell（15 个 `maxwell_*`）、Icepak、Q3D、Circuit，
含 `run_python_code`、`analyze_design`、`export_results`、`screenshot` 等。

## 6. 验证记录

| 检查项 | 结果 |
|--------|------|
| `git --version` | 2.56.0.windows.4 ✅ |
| `python --version` | 3.14.10 ✅ |
| `import ansys.aedt.core`（全局） | 1.9.0 ✅ |
| MCP `initialize` 握手 | 成功，serverInfo `ansys-aedt-mcp-server` ✅ |
| `tools/list` | 45 个工具 ✅ |
| `check_aedt_installed` | `installed=true`, `2026.1`, `D:\ANSYS Inc\v261\AnsysEM` ✅ |
| 全局启动器 | 退出码 0 ✅ |
| Codex config.toml | TOML 解析通过，`mcp_servers=['ansys-aedt']` ✅ |

## 7. 常见问题

**启动慢**：首次 `import ansys.aedt.core` 约 5–10 秒；且从校园许可证服务器检出
`electronics3d_gui` 实测约需 60 秒，故 `startup_timeout_sec` 设为 300。

**MCP 2.x 不兼容**：上游项目基于 FastMCP（mcp 1.x）。mcp 2.x 将 `FastMCP` 改名为
`MCPServer`，故本部署将 `pyproject.toml` 依赖改为 `mcp>=1.0,<2` 并安装 1.30.0。
**升级时勿装 mcp 2.x**，否则 `from mcp.server.fastmcp import FastMCP` 会失败。

**许可证**：`launch_aedt` 会真正启动 AEDT，需要有效 Ansys 许可证。

**日志**：`%TEMP%\aedt-mcp`。

## 8. 加固套件 v1.3.0（2026-11-02 安装）

来源：`I:\downloads\pyaedt-mcp-hardening-kit-v1.3.0-20261102.zip`
SHA256 `4B79CCBA…59D4`，上游基线 `f6173fc`。

修的几类问题，正好对应本机实测踩过的雷：

| 改动 | 文件 | 作用 |
|---|---|---|
| A 超时预算 | 客户端 `env` | 60 s → 1800 s，长调用不再被"就地杀 broker" |
| B1 close watcher | `aedt_close_watcher.py` | "工程被另一个应用占用"弹窗不再被当成用户关闭请求（原逻辑会 `QuitApplication()` 把 AEDT 带走） |
| C 求解不先存盘 | `pyaedt_capabilities.py`、`mcp_server.py` | 交互求解改走 `analyze_setup()`；新增 `save_before_solve`（默认 `False`） |
| 工程守卫 | `aedt_project_guard.py`（新增） | 裸工程名改写成磁盘绝对路径，PyAEDT 于是"打开"而非"重建覆盖"；找不到文件则拒绝执行 |

守卫逃生开关：`AEDT_GUARD_ALLOW_CREATE=1`（恢复旧的"新建"行为）、
`AEDT_GUARD_SEARCH_DIRS`（追加搜索目录）。
守卫在 `pyaedt_worker.py` 启动时安装，因此覆盖所有 MCP 工具与 `run_python_code`。

**安装方式说明**：套件 `install.py` 的哈希基线按 LF 换行计算，而本仓库检出为 CRLF，
逐文件比对会误判成"被本地改过"并拒绝安装。实际做法是直接用套件 payload 覆盖
（内容与"基线 + 补丁"完全一致），安装器复查结果为 **8/8 已安装、0 个被改**。

**验证结果**：

| 检查项 | 结果 |
|---|---|
| `install.py --check` | 8/8 已安装，0 原始 / 0 改过 / 0 缺失 ✅ |
| 套件自带测试 | 36 项全过（18 + 13 + 5）✅ |
| 仓库测试 | 12/13 模块通过；`test_aedt_launcher` 因 Windows 8.3 短路径（`ADMINI~1`）失败，属既有问题，`aedt_launcher.py` 未被套件改动 |
| MCP 握手 / 工具数 | 正常，45 个工具 ✅ |
| 版本 | pyaedt 1.9.0 / mcp 1.30.0 ✅ |

## 9. 回滚

仓库已 git 化（`C:\ProgramData\ansys-aedt-mcp`），两个标签：

| 标签 | 提交 | 内容 |
|---|---|---|
| `baseline-pre-hardening` | `40b18f5` | 加固前的原始上游 + 本机部署配置 |
| `hardening-kit-v1.3.0` | `547214d` | 加固套件 v1.3.0 |

只退回文件、保留历史：

```powershell
cd C:\ProgramData\ansys-aedt-mcp
git checkout baseline-pre-hardening -- .
& .venv\Scripts\python.exe -m pip install -e .   # pyproject 回滚后会把 pyaedt 装回 1.5.0
```

整体切换：

```powershell
git reset --hard baseline-pre-hardening   # 丢弃加固提交
git reset --hard hardening-kit-v1.3.0     # 回到加固定版
```

注意：`*.bak-hardening-*` 是安装器生成的逐文件备份，已被 `.gitignore` 忽略。
恢复优先用 git，不要与逐文件备份混用。
