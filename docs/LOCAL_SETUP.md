# AEDT MCP — 本机安装与配置记录

> 历史记录，仅供比对；跨机操作以 [2026-11迁移与仿真验收](2026-11_迁移与仿真验收.md) 为准。任何重装、删除、清锁、重启服务或恢复均需用户明确授权与备份。

本文件记录这台机器上 Ansys AEDT MCP 的实际安装状态，便于日后排障、升级和重装。
它不是上游文档；上游说明见同目录的 `README.zh-CN.md` / `README.md`。

## 1. 来源与版本

| 项目 | 值 |
| --- | --- |
| 上游仓库 | https://github.com/Cai-aa/CAE-Agent-Hub |
| 目录 | `MCP/Ansys/AEDT MCP` |
| 分支 / commit | `main` @ `194ef498e6efb1984cd65325e21e71087588a581`（2026-09-30） |
| 包版本 | `ansys-aedt-mcp 0.2.0` |
| 工具数量 | 45（其中 15 个 `maxwell_*`） |

源码是按 GitHub 文件清单逐个下载的（仓库整体约 110 MB，本目录只需 44 个文件），
**最初不是 git clone**。2026-10-21 为支持精确回滚，已在本地补建 git 仓库（见第 10 节）。

## 0. 文档索引

| 想了解什么 | 看哪里 |
| --- | --- |
| 装在哪、什么版本、环境变量、DSH 注册 | 本文件第 1–6 节 |
| 实机验证结果、踩过的坑 | 本文件第 7 节 |
| 重装 / 升级 | 本文件第 8 节 |
| 与官方 `ansys/pyaedt-mcp` 的对比 | 本文件第 9 节 |
| git 仓库与回滚 | 本文件第 10 节 |
| **长调用稳定性改进（A + B1）** | **`STABILITY_IMPROVEMENTS.md`** |
| 工程实践（启动、求解器、建模、物理判据） | `AEDT_AI_PLAYBOOK.md`（工作区） |

## 2. 安装布局

```text
D:\Application\aedt-mcp\
├── .venv\                     独立虚拟环境（见下）
├── mcp_server.py              MCP 入口（FastMCP stdio）
├── aedt_*.py / pyaedt_*.py    发现、启动、broker、后端
├── maxwell_*.py               Maxwell 专用工具
├── scripts\                   验收与旧工具栏清理脚本
├── tests\                     141 项离线单元测试 + live 验收
└── LOCAL_SETUP.md             本文件
```

虚拟环境由 **AEDT 自带的 CPython 3.10** 创建，因此本机无需另装 Python：

```text
基础解释器：D:\Application\ANSYS Inc\Electronics\v261\AnsysEM\commonfiles\CPython\3_10\winx64\Release\python\python.exe
              3.10.19
venv 解释器：D:\Application\aedt-mcp\.venv\Scripts\python.exe
```

> 注意：本机 `PATH` 上只有一个 `C:\Python314`，PyAEDT 的依赖链在 3.14 上
> 没有可靠 wheel。用 AEDT 自带 3.10 既避开版本风险，又与上游 `py>=3.10` 的验证
> 基线一致。venv 只读取该解释器，不会向其写入任何包，不污染 AEDT。

## 3. 本地修正（与上游的差异）

**`pyproject.toml` 第 7 行：`"mcp>=1.0"` → `"mcp>=1.0,<2"`。**

这是必须的本地修正。上游依赖声明没有上限，而 MCP SDK 2.x 移除了
`mcp.server.fastmcp`（改名为 `mcp.server.mcpserver.MCPServer`），因此
`pip install -e .` 会装到 `mcp 2.3.0` 并在导入时直接抛错：

```text
ModuleNotFoundError: No module named 'mcp.server.fastmcp'
```

当前 venv 实际锁定在 `mcp 1.30.0`。**重新安装时务必带上这个 `<2` 上限。**

## 3.1 依赖版本变更（2026-10 期间）

初始安装时 `pyproject.toml` 钉的是 `pyaedt==1.5.0`（上游原值），随后升级到 1.9.0；2026-11-20 跟踪上游稳定版升级为 **`pyaedt==1.10.0`**（`pyproject.toml` 与 venv 一致）。

2026-10-21 已复验：`pyaedt 1.9.0` + `mcp 1.30.0` 下**全部 141 项单元测试通过**
（1 项跳过），45 个工具注册正常。注意上游那个测试名字叫
`test_dispatched_kwargs_match_installed_pyaedt_150_signatures`，虽然名字里带 150，
但它是对**当前已安装** PyAEDT 的签名做断言，因此 1.9.0 下依然有效并通过。

当前实际版本：`ansys-aedt-mcp 0.2.0` / `mcp 1.30.0` / `pyaedt 1.10.0`。

**实机等价性验证（2026-10-21）。** 单元测试只能证明代码能跑，证明不了 1.9.0 驱动
真实 AEDT 的结果是否变化，因此做了同模型跨版本对比：WR-90 TE10 分别在
pyaedt 1.5.0 和 1.9.0 下建同一模型、求解、导出 Touchstone，逐频点比对。

| 项 | 结果 |
| --- | --- |
| 频点 | 81 / 81 全部匹配 |
| max \|ΔdB\|（S11/S21/S12/S22） | **0.000000** |
| max \|Δ\|S\|\| | **≈1e-15**（浮点噪声级） |
| 自适应收敛 Max ΔS | 两版同为 **0.0026176** |

结论：**pyaedt 1.9.0 把同一模型驱出逐位相同的结果，升级是安全的。**
对比脚本：`test-artifacts\compare_pyaedt_versions.py`
（基准 `WR90_Waveguide_pyaedt150\`，1.9.0 结果 `WR90_Waveguide_pyaedt190_fresh\`）。

（除 `mcp<2` 与 `pyaedt` 版本这两处外，其余文件与上游 commit 逐字节一致；
重新下载上游文件后需要重新施加这些修正。）

## 3.2 analyze_design 不再隐式保存工程（2026-10-21）

**改的是 `pyaedt_capabilities.py::_analyze_design` 与 `mcp_server.py::analyze_design`。**

上游在 `analyze_design` 里直接调 PyAEDT 的 `app.analyze(...)`，而
`ansys/aedt/core/application/analysis.py:1816` 的第一行是无条件的
`self.save_project()`。**更正：**在本机 HFSS / AEDT 2026 R1 的后续实测中，保存前后及重新连接后 Iterative 保持不变；先前“保存必然重置为Direct”的说法不成立。不同版本或求解器（包括 Icepak）行为不能由此推断：每次提交前、必要时保存后，均读取原生 Solver Type 验证。避免不必要的保存仍可降低意外改写/结果关联风险，但不构成备份。

本地改法：

- 交互模式（默认）改调 `app.analyze_setup(...)`，该函数体内没有任何
  `save_project()`，资源参数 `cores / tasks / gpus / acf_file / use_auto_settings`
  全部保留；
- 批量模式（`solve_in_batch=True`）保持 `app.analyze(...)`，因为批量求解本来就要先保存；
- 新增参数 `save_before_solve`（默认 `False`），需要旧行为时显式打开。

**详细根因、测试与回滚见 `STABILITY_IMPROVEMENTS.md` 第 6 节。**

## 3.3 工程覆盖守卫 aedt_project_guard（2026-10-21）

**新增文件**：`aedt_project_guard.py` + `tests/test_project_guard.py`（11 个用例）。
**改动文件**：`pyaedt_worker.py`（broker 启动时安装）。

背景：`Hfss(project="名字")` 在工程未打开时会**新建空工程并覆盖同名 `.aedt`**，
2026-10-19 / 10-21 两次因此丢失 `LensFeed.aedt`、`LensFeedlz.aedt`、`LensFinal.aedt`。

做法：包装 14 个 PyAEDT 应用类的 `__init__`，把裸工程名改写为磁盘上已存在
`.aedt` 的绝对路径（PyAEDT 于是打开而非重建）；找不到文件则抛 `RuntimeError`
并给出可操作的提示。因为是 broker 启动时安装，**所有 MCP 工具调用与
`run_python_code` 在全部会话中都受保护**；直接用 venv python 跑的脚本需自行
`import aedt_project_guard; aedt_project_guard.install()`。

逃生开关：`AEDT_GUARD_ALLOW_CREATE=1`（恢复旧行为）、
`AEDT_GUARD_SEARCH_DIRS`（追加搜索目录）。

**注意**：`install()` 会提前 import PyAEDT，其 stdout 横幅会破坏 broker 的
单行 JSON 协议；`install()` 已把 import 期的输出重定向到 stderr（commit `8e681d8`）。
修改本模块后必须重跑 `python -m unittest tests.test_project_guard`。

## 4. 环境变量

已在 **用户级**（`HKCU\Environment`）全局设置，供命令行、其他 MCP 客户端和排障使用：

| 变量 | 值 |
| --- | --- |
| `AEDT_VERSION` | `2026.1` |
| `AEDT_INSTALL_DIR` | `D:\Application\ANSYS Inc\Electronics\v261\AnsysEM` |
| `AEDT_LOG_DIR` | `C:\Users\lingze\AppData\Local\Temp\aedt-mcp` |
| `AEDT_WORKER_TIMEOUT` | `60` |
| `AEDT_LAUNCH_TIMEOUT` | `120` |

`AEDT_VERSION=2026.1` 与机器级已有的 `ANSYSEM_ROOT261` 一一对应
（版本号会映射成 `ANSYSEM_ROOT261`），两处指向同一目录。

> 项目只读 `os.environ`，**不会自动读取 `.env` 文件**，所以同目录的
> `.env.example` 只是个模板，没有生效；真实配置就是上面的用户级变量
> 加上第 5 节的客户端 `env` 块。

## 5. 客户端注册（DSH）

注册写在 DSH 的**机器级补丁层**，对所有 profile 生效：

```text
C:\Users\lingze\.dsh\cordis.patch.yml
```

该文件由 DSH 的 `watchUserPatches()` 通过 Cordis HMR 监听，**改完即时生效、无需重启**。
（注意：web profile 的 bundle 把 `hmr` 那一行标为 `disabled: true`，但 DSH 在启动时
会自行创建 HMR 插件来监听用户补丁层，所以热加载依然可用。）

补丁层按顺序叠加：bundle 层 → profile 自己的 `cordis.patch.yml` → **本机器级层** → `--patch`。
一个 `insert` 行只应在一个层里出现一次：同一个 `serverName` 注册两次会让后一个实例加载失败。

注册后的工具名形如 `mcp__ansys-aedt__check_aedt_installed`。

两个关键点，改动时不要丢：

1. **`env` 必须显式声明。** MCP 客户端用 *scrubbed* 环境启动子进程（凭据形态的变量和
   过期的 `DSH_*` 会被剔除，`PATH` 不保证存在），所以不能依赖继承。
2. **`toolCallTimeoutMs: 900000`（15 分钟）。** 客户端默认 60 秒，而单次
   `launch_aedt` 就可能阻塞 `AEDT_LAUNCH_TIMEOUT`（120 秒），求解与导出更久。

## 6. AEDT 侧：无需安装任何插件

**当前上游版本在 AEDT 内不安装任何东西。** 架构是：

```text
MCP 客户端 → FastMCP stdio server → 外部 PyAEDT broker → AEDT gRPC
```

AEDT 由 `launch_aedt` 以 `ansysedt.exe -grpcsrv <port>` 启动；手动打开的 AEDT 需要
自带监听端口，用 `list_aedt_sessions` 查 PID。系统**没有隐式默认会话**，每个目标工具
必须且只能传 `pid` 或 `port` 之一。

上游曾在 `2026-06-27` 的 commit `9976c745`（*Remove the resident AEDT bridge*）中删除了
旧的常驻方案：`aedt_mcp_bridge.py`、`scripts/install_aedt_toolkit_button.ps1`、
`install_aedt_mcp_autostart.ps1` 都已不存在，同时新增了
`scripts/remove_legacy_aedt_mcp_toolbar.ps1` 用于清理旧的 `Start/Stop AEDT MCP Bridge`
工具栏按钮。

本机检查结果：**没有遗留条目**，`syslib\Toolkits` 未被改动。

```powershell
# 只读复查（DryRun，不会改文件）
& "D:\Application\aedt-mcp\scripts\remove_legacy_aedt_mcp_toolbar.ps1" `
    -AedtRoot "D:\Application\ANSYS Inc\Electronics\v261\AnsysEM" -DryRun
# 期望输出： HFSS: would remove 0 known XML node(s). / Project: would remove 0 ...
```

## 7. 已验证 / 未验证

### 离线验证（全部通过）

- `python -m unittest discover -s tests -v` → **141 项通过，1 项跳过**
  （跳过的是需要真实 AEDT 的 live 测试）。
- `mcp_server` 导入成功，注册 **45** 个工具，其中 `maxwell_*` **15** 个。
- 经 DSH 端到端调用 `check_aedt_installed` → 正确返回 AEDT 2026.1 + PyAEDT 1.10.0。
- `list_aedt_sessions` → 正确返回空列表 + `selection_required: true`。

### 实机验证 1：联通与写控制（通过）

对一个从 GUI 手动打开、未用 `launch_aedt` 启动的 AEDT 2026.1 会话：

- `check_aedt_connection` 按 `port=50051` 与按 `pid` 均连通，PID 自动解析到同一端口。
- `get_project_info` / `list_projects` / `list_designs` 正确读到活动工程。
- `create_design` 建出 HFSS 设计；`get_model_info` 回读 `design_type=HFSS`。
- `run_python_code` 建出真实几何（`app.modeler.create_box` → `MCP_Box`）。
- `validate_design` 对裸 box 正确报出「缺少激励 / 缺少求解设置」两条错误。
- `close_projects(save=false)` 只关目标工程；`release_connection` 释放 broker 并保留
  AEDT 窗口。

> 该会话**自带 gRPC 监听在 50051**，无需用 `-grpcsrv` 启动。

### 实机验证 2：WR-90 TE10 完整案例（通过）

`build_wr90_waveguide`（`port=50051`）一次调用完成建模 → 校验 → 求解 → 导出：

- 几何：WR-90（22.86 × 10.16 × 50 mm）PEC 壁 + 真空通道，`subtract` 成功。
- 端口：`WavePort1` / `WavePort2`；设计校验 **PASSED**，识别 2 个激励。
- Setup1 @ 10 GHz，Max Passes 6；Sweep8to12GHz：8–12 GHz、81 点、Interpolating。
- 收敛：2 次自适应，**Max Mag. Delta S = 0.0026**（目标 0.02），`Converged : Yes`，
  824 个单元。
- 导出：`.prof`、`.conv`、`.s2p`（81 点 MA 格式）均生成。
- 独立复核（`test-artifacts/check_wr90.py` 直接解析 Touchstone，不依赖 AEDT）：
  全频段 **|S21| = 0.000 dB**、**|S11| 最差 −50.2 dB**。TE10 截止 6.557 GHz，
  整段扫频落在单模通带内 —— 与教科书结果一致。

**未验证**：Icepak 热仿真；Maxwell 电机类瞬态（网格/时间步收敛与工程结论仍需专门
验证，见上游 `MAXWELL_VALIDATION.md`）；`-Mode both` 的完整实机验收（含「broker
连接中正常关闭 AEDT」这一项）。需要时：

```powershell
cd D:\Application\aedt-mcp
.\scripts\run_live_acceptance.ps1 -Mode both `
    -AedtRoot "D:\Application\ANSYS Inc\Electronics\v261\AnsysEM"
```

## 7.1 三个已踩过的坑

### 坑 1：传了已退出的 PID，报错却说是版本不匹配

`build_wr90_waveguide(pid=<已退出的 PID>)` 会失败，且信息**完全误导**：

```text
The version specified (2026.1) doesn't correspond to the pid specified ({self.aedt_process_id})
```

两点原因都在 PyAEDT（`ansys/aedt/core/desktop.py`）：

1. 真正的条件是 `Desktop.check_starting_mode()`（L1136-1147）里
   `self.aedt_process_id in active_sessions(version=..., non_graphical=...)` 不成立，
   即**该 PID 已不存在**，与版本无关。
2. L1145-1146 是 `f"..." "..."` 拼接，**第二段是普通字符串**，因此
   `{self.aedt_process_id}` 永远不会被替换。这是 PyAEDT 自身的 f-string bug。

`active_sessions()` 先靠**命令行**里的 `-grpcsrv` 解析端口，再退回 psutil TCP 扫描；
版本过滤是拿规范化后的 `"261"` 对命令行做子串匹配。从 GUI 正常启动的 AEDT 命令行里
没有 `-grpcsrv`，只能靠 TCP 兜底 —— 能用，但依赖进程存活。

**规避**：用 PID 前先调 `list_aedt_sessions` 取**当前** PID，不要复用缓存值；对不是
自己用 `launch_aedt` 启动的会话，用 `port` 更稳。

### 坑 2：`non_graphical` 是硬过滤

实测 `active_sessions(version="2026.1", non_graphical=True)` 对可见会话返回 `{}`。
从 GUI 打开的 AEDT 必须传 `non_graphical=False` 或不传。

### 坑 3：`run_python_code` 里要用 PyAEDT API，别用原生对象

```python
# 会被 AEDT 2026 R1 的 gRPC 层拒绝：
#   GrpcApiError: Failed to execute gRPC AEDT command: CreateBox
oEditor = design.SetActiveEditor("3D Modeler")
oEditor.CreateBox([...], [...])

# 正确写法（也是上游 wr90_case.py 的写法）：
from ansys.aedt.core import get_pyaedt_app
app = get_pyaedt_app(project_name=..., design_name=..., desktop=desktop)
app.modeler.create_box([0, 0, 0], [10, 10, 10], name="Box1", material="vacuum")
```

`get_model_info` 在无活动设计时会报
`Failed to execute gRPC AEDT command: GetName` —— 预期行为（没有东西可读），不是故障。

### 坑 4：设了全局 `AEDT_INSTALL_DIR` 后，单元测试会有 1 项假失败

在**设置了** `AEDT_INSTALL_DIR` 的环境里跑测试：

```text
FAIL: test_resolve_executable_from_install_dir_or_environment
AssertionError: WindowsPath('D:/Application/ANSYS Inc/.../ansysedt.exe')
             != WindowsPath('C:/Users/.../tmpi_xxxx/ansysedt.exe')
```

这不是回归。`resolve_aedt_executable()` 的查找顺序是
**`AEDT_INSTALL_DIR` → `ANSYSEM_ROOT<code>`**，而该用例只把 `ANSYSEM_ROOT261`
指向临时目录、没清 `AEDT_INSTALL_DIR`，于是全局变量赢下了这一轮。

A/B 实测（2026-10-21）：

| 环境 | 结果 |
| --- | --- |
| 未设 `AEDT_INSTALL_DIR` | **141 项 OK**（1 skipped） |
| 设了 `AEDT_INSTALL_DIR` | 141 项，**1 failed**（即上面这条） |

**跑测试时先清掉它**：

```powershell
$env:AEDT_INSTALL_DIR = $null
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

注意 DSH 的 MCP 子进程**必须**保留该变量（见第 5 节），所以只在跑测试的 shell 里清。

### 坑 5（重要）：`analyze_setup` 返回 True 不代表求解真的跑了

2026-10-21 复现：对一个**已连续运行约 15 小时**的 AEDT 会话（PID 33140）调用
`build_wr90_waveguide`，建模和校验都正常，求解阶段失败：

```text
[error] Unable to start analysis on machine 'Local Machine' :
[warning] Com Engine non-responsive since 17:17:36 ... Retrying.....
[error] Script macro error: Solution data is not available.
```

但 PyAEDT 的日志却是**成功**的：

```text
INFO : Solving design setup Setup1
INFO : Design setup Setup1 solved correctly in 0.0h 2.0m 1.0s      <- 假的
```

那 2 分钟全花在重试失败的启动上，`analyze_setup(blocking=True)` 仍然返回 `True`，
于是流程继续往下走，直到 `ExportNetworkData` 才报错：

```text
Failed to execute gRPC AEDT command: ExportNetworkData
```

**排除了授权问题**：`lmutil lmstat -c 1055@localhost -a` 显示 `hfss_solve`、
`elec_solve_hfss` 等各有 500 个可用、**0 个在用**，只有 GUI 类被占用。

**排除后定位为会话僵死**：用 `launch_aedt` 起一个全新会话（非图形模式）后，
同一调用一次成功。所以处理办法是：**遇到这种症状先换会话，不要怀疑代码或授权。**

```powershell
# 快速确认求解器是否真的起来了：正常求解期间应能看到求解器进程
Get-Process | Where-Object Name -match "ansysedtengine|hfss"
```

**教训**：判断"求解完成"永远不要依赖返回值，要查证据——`.conv` 里的
`Converged : Yes`、`.prof` 的实际时长与单元数、以及解数据是否存在。
这与上游 README 的告警一致：`started=true` 只表示任务已提交。

## 7.2 本机运行产生的文件位置

| 内容 | 路径 |
| --- | --- |
| WR90 成果 · pyaedt 1.5.0 基准 | `test-artifacts\WR90_Waveguide_pyaedt150\` |
| WR90 成果 · pyaedt 1.9.0 | `test-artifacts\WR90_Waveguide_pyaedt190_fresh\` |
| 物理复核脚本 | `test-artifacts\check_wr90.py` |
| 跨 PyAEDT 版本 S 参数对比 | `test-artifacts\compare_pyaedt_versions.py` |
| PID 寻址诊断脚本 | `test-artifacts\diag_pid_target.py` |
| 官方版对比脚本 | `test-artifacts\compare_mcp.py`、`test-artifacts\diff_official.py` |
| 官方版源码快照（对比用） | `test-artifacts\official\` |
| AEDT 默认工程目录 | `D:\HFSS Temp\`（AEDT 每次新建工程都会在此留一个同名骨架 `.aedt`） |

`D:\HFSS Temp\` 是**用户 HFSS 工程的实际工作目录**（`Lens*`、`MetalRingLens*`、
`Project4-7`、`hfss_*_*.pjt` 等都在其中）。里面的 `Project*.aedt.lock`、`PersonalLib`、
`TwBError`、`Maxwell` 均属 AEDT 自身或用户工程，**一律不要清理**。

> `check_wr90.py` 与 `compare_pyaedt_versions.py` 里写死了上述目录路径，
> 换目录运行前需改 `BASE` / `NEW` 常量。

## 8. 重装 / 升级

```powershell
$py = "D:\Application\ANSYS Inc\Electronics\v261\AnsysEM\commonfiles\CPython\3_10\winx64\Release\python\python.exe"
& $py -m venv D:\Application\aedt-mcp\.venv
& D:\Application\aedt-mcp\.venv\Scripts\python.exe -m pip install -U pip
& D:\Application\aedt-mcp\.venv\Scripts\python.exe -m pip install -e D:\Application\aedt-mcp
# 再次确认没有跳到 mcp 2.x：
& D:\Application\aedt-mcp\.venv\Scripts\python.exe -c "from mcp.server.fastmcp import FastMCP; import importlib.metadata as m; print('mcp', m.version('mcp'), 'OK')"
```

升级上游源码后：重新施加第 3 节的 `mcp<2` 修正，然后重启 MCP 服务或改一下
`cordis.patch.yml` 触发 HMR；已打开的 AEDT 窗口可以保持打开。

**授权**：安装 MCP 不等于获得求解器许可。本机 license 由 `ANSYSLMD_LICENSE_FILE=1055@localhost`
提供，AEDT 能否真正求解取决于该 license server 的状态。

## 9. 官方 `ansys/pyaedt-mcp` 对比评估（2026-10-21 核查）

上游官方仓库是 https://github.com/ansys/pyaedt-mcp ，PyPI 包名 `ansys-aedt-mcp`，
**与本机安装的包同名但内容不同**。结论：**不作为替换，保留当前安装。**

### 事实对比

| 维度 | 当前安装（CAE-Agent-Hub） | 官方 `ansys/pyaedt-mcp` |
| --- | --- | --- |
| 版本 | 0.2.0（快照 `main`@`194ef49`） | 0.2.4（PyPI，2026-10-01） |
| 许可 | MIT（CAE-Agent-Hub） | Apache-2.0 |
| 工具数 | **45** | **22** |
| 维护状态 | 第三方快照，2026-09-30 后未更新 | 官方，当日仍有提交；0.1.1→0.2.4 共 14 个版本 |
| Python 要求 | `>=3.10`（本机用 AEDT 自带 3.10.19） | PyPI 0.2.4 为 `>=3.12,<4`；仓库 main 为 `>=3.13,<4` |
| 依赖 | `mcp>=1.0`（本机锁 `<2`）、`pyaedt==1.10.0`、`psutil` | `pyaedt[graphics]>=1.3.0`、`ansys-common-mcp>=0.3.3`、`typer==0.27.3` |
| 传输 | stdio | stdio + HTTP |
| 安装方式 | 源码 + venv | PyPI / `uvx` / Windows 安装器 |

### 工具差异（实测枚举，非推测）

- **共同**：21 个（官方基线，当前安装是它的超集）
- **仅当前安装有：24 个**，切换即失去：
  - 全部 15 个 `maxwell_*`（线圈/绕组/电流/力/转矩/涡流/运动带/局部网格/场图/设置/结果）
  - `build_wr90_waveguide`（已实机验证的完整 HFSS 流程）
  - 显式目标 broker 控制：`list_aedt_sessions`、`check_aedt_connection`、`release_connection`
  - HFSS 工作流：`create_hfss_design`、`start_analysis`、`get_analysis_status`
  - 工程检查与清理：`get_project_info`、`close_projects`
- **仅官方有：1 个** —— `display_html`（MCP Apps 内联 HTML，不支持时回退到独立浏览器窗口）

官方 0.2.x 的发布内容是工程加固（wheelhouse 离线安装、localhost 绑定与安全增强、
桌面 app 构建修复）和依赖升级，**没有新增 AEDT 能力**；其 changelog 从未出现
Maxwell/Icepak 专用工具族。

### 为什么结论是「不替换」

1. 官方 22 个工具正是当前 45 个工具的子集；替换是**净减 24 个能力换 1 个**。
2. 官方用隐式「当前会话」状态；当前实现刻意要求显式 PID/port，多会话时更安全
   （本次实践也验证了这一点）。
3. 官方要求 Python 3.12+/3.13+，而本机 venv 是 AEDT 自带的 3.10，替换需另装
   Python 并重建环境，且 `pyaedt[graphics]` 依赖更重。
4. 当前安装已在同一台机器上用真实 AEDT 2026.1 跑通 WR-90 求解并核对过物理结果
   （第 7 节），替换需要重做全部验收。

### 官方的真实优势（值得关注，但不构成替换理由）

- 官方长期维护：共享的 21 个工具未来若适配 AEDT 2027 R1，只有官方会跟进。
- 每工具粒度超时（`_TIMEOUT_QUICK/MEDIUM/LONG`）、连接锁（`locked_connection`）、
  动态工具隐藏（`REQUIRES_AEDT_TAG`）。
- HTTP 传输、`uvx` 免克隆运行、Windows 安装器、独立的 Sphinx 文档站。
- `display_html` + `html_app_view` 资源（结果可视化）。

若日后需要这些，**建议新增为第二个 MCP server**（不同 `serverName`，独立 venv 与
Python 3.13），而不是替换；代价是约 21 个重复工具定义使每轮请求的 schema 开销翻倍。

### 包名冲突警告

两个发行包都叫 `ansys-aedt-mcp`，本机是 **editable 安装**且版本 0.2.0。
实测 `pip install -U ansys-aedt-mcp` **不会**自动从 PyPI 升级（pip 判定已满足）。
但以下命令会**用官方包覆盖当前安装、直接丢掉 24 个工具**，不要执行：

```powershell
pip install --force-reinstall ansys-aedt-mcp
pip install "ansys-aedt-mcp==0.2.4"
pip install --upgrade --ignore-installed ansys-aedt-mcp
```

要升级本方案，请回到第 8 节用 `-e`（editable）重装，或重新下载 CAE-Agent-Hub 源码。

## 10. 版本控制（本目录现在是 git 仓库）

初始状态下本目录**不是** git 仓库（44 个文件是按 GitHub 清单逐个下载的）。2026-10-21
为「改动可精确回滚」补建了仓库。

### 10.1 Git 本体

| 项 | 值 |
| --- | --- |
| 版本 | **git 2.56.0.windows.2** |
| 类型 | **PortableGit**（官方 git-for-windows 发行包，免安装、免管理员） |
| 位置 | `D:\Application\Git` |
| 入口 | `D:\Application\Git\cmd\git.exe` |
| PATH | 已追加 `D:\Application\Git\cmd` 到**用户级** PATH |

选择 PortableGit 的原因：本机 `winget` / `choco` 都在，但两者安装 Git 都需要提权，
而当时会话**不是管理员**且无审批通道，会卡在 UAC。PortableGit 是官方同源发行包，
不需要管理员，**卸载 = 删除目录 + 去掉 PATH 条目**。

> 注意事项：改用户级 PATH 只写注册表，**已经在运行的进程不会重建环境块**。
> 所以老终端里 `git` 仍不可见，需新开终端（或直接用上面的完整路径）。
> 已广播 `WM_SETTINGCHANGE` 让资源管理器刷新。

### 10.2 仓库

| 项 | 值 |
| --- | --- |
| 基线提交 | `f6173fc` — *baseline: upstream AEDT MCP + local install notes* |
| 跟踪文件 | 47 个（约 793 KB） |
| 分支 | `main` |
| 身份 | **仅仓库内**：`aedt-mcp-local <aedt-mcp@localhost>`（未改全局 git 配置） |

关键配置：`core.autocrlf=false` + `core.eol=lf`。**这是必要的**——默认的
autocrlf 会让 checkout 把 LF 改写成 CRLF，回滚就不是字节原样了。已用往返测试验证
（改文件 → `git checkout --` → SHA256 完全一致）。

`.gitignore` 在原有 `test-artifacts/` 基础上补充了 `.venv/`、`__pycache__/`、
`*.py[cod]`、`*.egg-info/` 以及工具自身产生的 `batch.log`、`screenshot.jpg`。
这是本目录与上游的**又一处在差异**（第 3 节之外），重新下载上游文件时会覆盖回去。

### 10.3 回滚用法

```powershell
cd D:\Application\aedt-mcp
$git = "D:\Application\Git\cmd\git.exe"

# 看改了什么
& $git status
& $git diff                     # 未暂存的改动
& $git diff f6173fc             # 相对基线的全部改动

# 撤销全部改动，回到基线（字节保真）
& $git checkout -- .

# 若已提交，也想退回基线
& $git reset --hard f6173fc

# 只撤销某个文件
& $git checkout -- aedt_close_watcher.py
```

改完源码后需**重启 MCP 服务**（或触发 `cordis.patch.yml` 的 HMR）才生效，
因为 broker 是独立进程、启动时加载模块。


## 2026-11-20 PyAEDT 1.10.0 live attach check

- Client: PyAEDT 1.10.0 against AEDT 2026.1 (gRPC, explicit PID).
- ping -> connected=true, aedt_version=2026.1; list_projects -> empty list; desktop release -> True.
- The attach-only protection audited path (_validate_port, __initialize, __dispatch_win32, check_starting_mode) is present in the 1.10.0 Desktop class.
- Offline suite after the upgrade: 458 passed, 1 skipped.
