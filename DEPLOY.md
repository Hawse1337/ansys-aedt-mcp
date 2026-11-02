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
| 虚拟环境内 PyAEDT | 1.5.0（MCP 官方锁定版本） | 同上 |
| MCP SDK (FastMCP) | 1.30.0 | 同上 |
| Ansys AEDT | 2026 R1 (v261) | `D:\ANSYS Inc\v261\AnsysEM\ansysedt.exe` |

> 说明：MCP 项目的 `pyproject.toml` 锁定 `pyaedt==1.5.0`（官方 PyAEDT MCP 能力基线），
> 因此 MCP 使用**独立虚拟环境**；系统 Python 中另装最新版 pyaedt 1.9.0 供日常脚本使用。
> 两者互不干扰。

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
  startup_timeout_sec = 120
  tool_timeout_sec = 600
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
| `AEDT_WORKER_TIMEOUT` | `60` |
| `AEDT_LAUNCH_TIMEOUT` | `120` |
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

**启动慢**：首次 `import ansys.aedt.core` 约 5–10 秒，故 `startup_timeout_sec` 设为 120。

**MCP 2.x 不兼容**：上游项目基于 FastMCP（mcp 1.x）。mcp 2.x 将 `FastMCP` 改名为
`MCPServer`，故本部署将 `pyproject.toml` 依赖改为 `mcp>=1.0,<2` 并安装 1.30.0。
**升级时勿装 mcp 2.x**，否则 `from mcp.server.fastmcp import FastMCP` 会失败。

**许可证**：`launch_aedt` 会真正启动 AEDT，需要有效 Ansys 许可证。

**日志**：`%TEMP%\aedt-mcp`。
