# AEDT MCP 长调用稳定性改进（A + B1 + C）

> 旧版实验记录；2026-11更新与安全边界以 [跨机迁移与仿真验收](2026-11_迁移与仿真验收.md) 为准。本文中的历史恢复命令不得未经审查和用户授权直接执行。

> 日期：2026-10-21
> 范围：`D:\Application\aedt-mcp`（MCP 服务端）与 `C:\Users\lingze\.dsh\cordis.patch.yml`（DSH 客户端配置）
> 性质：**本地对上游的修改**。上游 `main` @ `194ef498` 不自带这些改动，重新下载源码后需按本文重新施加。
> 关联文档：`LOCAL_SETUP.md`（安装与运维总记录）、`AEDT_AI_PLAYBOOK.md`（工程实践手册）

---

## 1. 摘要

| 改动 | 内容 | 文件 | 提交 |
| --- | --- | --- | --- |
| **A** | 把每次工具调用的超时预算从 60 s 提到 1800 s；客户端预算提到 40 min | `cordis.patch.yml` | 不在 git（DSH 目录） |
| **B1** | close watcher 不再把「另一个客户端占用」的弹窗当成用户关闭；改为「主窗口消失 + 持续 1 s」才算 | `aedt_close_watcher.py` + 其测试 | 见 `git log` |
| **C** | `analyze_design` 不再隐式保存工程（PyAEDT 的 `analyze()` 第一行就是无条件 `save_project()`；求解前写盘会顺带清理其它设计的结果目录，且多一次 IO。~~早先说"保存会重置 Solver Type"，2026-10-22 实测推翻~~）| `pyaedt_capabilities.py` + `mcp_server.py` + 测试 | 见 `git log` |

**效果**：消除了「长调用 → broker 被杀 → gRPC 会话损坏 → AEDT 被自动退出」这条自伤链的两端。

---

## 2. 症状（用户报告）

> "调用工具时间长经常会出现 gRPC 不稳定的情况"

具体表现（来自 `AEDT_AI_PLAYBOOK.md` 第 2.3–2.5 节，以及本次会话中的实测）：

- 长调用之后随机报 `GrpcApiError`（`GetChildObject` / `OpenProject` / `GetProjectList` / `EditSetup` 等）
- `GetMessages` 返回 0 条
- `SetPropValue` 静默无效，不报错但值不变
- `Analyze` 返回「已提交」，但 `hf3d.exe` 从不出现
- 消息窗口出现 `Com Engine non-responsive`、`Unable to start analysis on machine 'Local Machine'`
- 偶尔出现 `Simulation was aborted by user on server`

**这些都不是随机故障**，可以归到一条确定的因果链上。

---

## 3. 根因：四段自伤链

### ① 绝大多数工具只有 60 秒预算

`WorkerClient` 的默认超时取自环境变量 `AEDT_WORKER_TIMEOUT`（缺省 `"60"`），见
`worker_client.py:71`：

```python
self.default_timeout = float(
    default_timeout
    if default_timeout is not None
    else os.environ.get("AEDT_WORKER_TIMEOUT", "60")
)
```

而 `mcp_server.py` 里绝大多数工具写的是 `timeout=timeout`（即 `None`），直接落到这个
默认值。**只有三个工具写死了更大的值**：

| 工具 | 位置 | 默认超时 |
| --- | --- | --- |
| `analyze_design` | `mcp_server.py:520` | 600 s |
| `export_results` | `mcp_server.py:543` | 600 s |
| `build_wr90_waveguide` | `mcp_server.py:723` | 1800 s |

**其余 42 个工具全是 60 秒**，包括最常用的 `run_python_code`、`open_project`、
`create_design`、`save_project`、`screenshot`、`get_project_info`，以及全部 15 个
`maxwell_*`。

对照实际工作量：手册里记录的工程是 2.8–3.4 MB、5–31 个设计，单次求解约 20 分钟，
结果目录每份约 240 MB。这些都远超 60 秒。

### ② 超时的处置是「就地杀掉 broker」

`worker_client.py:230–231`：

```python
except queue.Empty as exc:
    self._stop_broker(broker, graceful_timeout=0.5)
    raise WorkerTimeoutError(
        f"AEDT broker timed out after {timeout:g}s for {request.target.key}"
    ) from exc
```

broker 就是持有 PyAEDT 连接的那个子进程。超时 = **在操作进行到一半时把它杀掉**。

**这一点在本次会话中有直接观测**：改造之前调用 `list_projects` 时收到过

```
Error: Error executing tool list_projects: AEDT broker timed out after 60s for port:50051
```

### ③ 杀掉客户端正好违反 AEDT 2026 R1 的 gRPC 契约

上游 README 明确写着：

> AEDT 2026 R1 的 gRPC 会话要求客户端持续存在；如果每条命令后都结束 PyAEDT 客户端，
> 对应 gRPC 监听也会消失。

也就是说第 ② 步杀掉 broker，会把 AEDT 侧那个 gRPC 会话打成**半持有**状态——
这正好解释为什么后续调用开始随机报 `GrpcApiError`。

### ④ 下一次调用触发「被占用」弹窗，然后被自动退出

下一个工具调用会起一个**新的** broker 去连同一个 AEDT，于是 AEDT 弹出：

```
This project is being used by another application, script or extension wizard
```

而 `aedt_close_watcher.py:11` 正是拿这个字符串当判定依据。改造前
的 `poll_once()` 是：

```python
state = self._window_state(pid)
if state.busy_dialog:
    self._trigger("busy_dialog")        # 第一次看到就触发，没有防抖
elif state.main_visible:
    self._seen_main_window = True
elif self._seen_main_window:
    self._trigger("main_window_closed")
```

`on_close_intent` 最终会走到
`pyaedt_worker.py:73–77 → handle_close_intent → backend.close_for_user_request()`
→ 调用 AEDT 的 `QuitApplication()`。

**于是「另一个客户端在占用」被误判成「用户要关闭」，AEDT 被中途退出。**

### 链路汇总

```
长调用(>60s)
   → worker_client 超时，杀掉 broker            [worker_client.py:230]
   → AEDT 的 gRPC 会话失去客户端                [上游 README 明示]
   → 下次调用起新 broker，两个客户端争同一次会话
   → AEDT 弹 "being used by another application"
   → close watcher 判定为用户关闭               [aedt_close_watcher.py:原 poll_once]
   → QuitApplication()，AEDT 中途被杀            [pyaedt_worker.py:73-77]
   → 此后一切 gRPC 调用失败（表象："gRPC 不稳定"）
```

**注意因果关系**：真正致命的不是"超时"本身，而是「超时」被第 ④ 步放大成
「AEDT 被杀」。所以 A 和 B1 分别是链条的两端，缺一不可。

---

## 4. 改动 A：抬高超时预算

文件：`C:\Users\lingze\.dsh\cordis.patch.yml`（DSH 机器级补丁层，对所有 profile 生效）

| 键 | 原值 | 新值 | 说明 |
| --- | --- | --- | --- |
| `AEDT_WORKER_TIMEOUT` | `'60'` | `'1800'` | 服务端每请求预算（30 min） |
| `AEDT_LAUNCH_TIMEOUT` | `'120'` | `'240'` | 启动 AEDT 的等待上限 |
| `toolCallTimeoutMs` | `900000` | `2400000` | 客户端每调用预算（40 min） |

同时把**用户级环境变量** `AEDT_WORKER_TIMEOUT` / `AEDT_LAUNCH_TIMEOUT` 同步为
1800 / 240（仅供手动 CLI 使用；MCP 子进程用的是 yml 里的显式 `env`，
因为父环境是被 scrubbed 的）。

### 为什么客户端预算必须严格大于服务端

`2400000 ms = 40 min > 1800 s = 30 min`。

如果反过来（客户端先放弃），会出现：客户端报超时但服务端仍在跑 → 操作员重试
→ 第二个 broker 连同一个 AEDT → 正是第 ④ 步的触发条件。
**顺序必须是「服务端先失败」**，这样客户端拿到的永远是一个确定的错误。

### 代价与取舍

- 好处：真正耗时的工作不再被误杀。
- 代价：若 AEDT 真的卡死，单次调用最长会挂 30 分钟才报错（旧值是 1 分钟）。
- 缓解：B1 已经切断了"卡死→被杀"的放大；且 `f6173fc` 基线可随时回退。
- 想更快失败：把 `AEDT_WORKER_TIMEOUT` 调到 `600` 即可，其余不变。

---

## 5. 改动 B1：close watcher 不再误杀

文件：`aedt_close_watcher.py`

### 改动内容

新增常量 `DEFAULT_BUSY_DIALOG_CONFIRMATIONS = 5`（`aedt_close_watcher.py:23`），
并在构造函数新增 `busy_dialog_confirmations` 参数（`:81`）。

`poll_once()`（`:100–128`）的新逻辑：

```python
state = self._window_state(pid)
if state.main_visible:
    self._seen_main_window = True

# 主窗口还在时出现的 busy 弹窗 = 另一个客户端占用的正常症状，不是关闭请求
if state.busy_dialog and not state.main_visible:
    self._busy_dialog_polls += 1
    if self._busy_dialog_polls >= self._busy_dialog_confirmations:
        self._trigger("busy_dialog")
    return

self._busy_dialog_polls = 0
if not state.main_visible and self._seen_main_window:
    self._trigger("main_window_closed")
```

判定条件从「看到 busy 弹窗就触发」收紧为：

**主窗口已经不可见**（`not main_visible`）**且弹窗持续 ≥ 5 次轮询**（默认 0.2 s
一轮，即 ≥ 1 秒）才判定为用户关闭。

另外新增 `_reset()`（`:95`）统一重置三个状态（pid、主窗口可见标志、busy 计数），
避免 pid 变化时计数残留。

### 为什么这样改（而不是别的方案）

被评估但未采用的方案：

| 方案 | 否决理由 |
| --- | --- |
| 只加防抖（无论主窗口状态，持续 1 s 就触发） | **无效**：真正的客户端争用会持续几十秒，防抖照样会被击穿 |
| 完全删掉 busy 弹窗判定 | 会丢掉上游的设计意图——用户关闭 AEDT 后残留模态窗时，broker 会变成孤儿进程 |
| 只在「无活跃请求」时才解释为关闭 | 有效但耦合过深：watcher 需要知道 worker 的请求状态，改动面扩大到 `pyaedt_worker.py`，违背最小改动原则 |

采用的方案用 `WindowState` 里**已经存在**的信息（`main_visible`）区分两种语义，
不需要跨模块耦合：

- 弹窗 + 主窗口可见 → 客户端争用（**新：只忽略**）
- 弹窗 + 主窗口消失 → 用户关闭后的残留（**保留原意图，加上防抖**）

### 对测试的影响（必须一起回滚的唯一耦合点）

原测试 `test_busy_dialog_triggers_close_without_waiting_for_hidden_window`
断言的正是「单次 busy 弹窗立即触发」，与新行为直接冲突，已被替换为
`test_busy_dialog_with_visible_main_window_is_not_a_close_request`。

测试文件 `tests/test_aedt_close_watcher.py` 从 3 个用例变为 5 个：

| 用例 | 覆盖 |
| --- | --- |
| `test_visible_to_hidden_transition_triggers_close_once` | 原有行为不变（主窗口可见→消失） |
| `test_busy_dialog_with_visible_main_window_is_not_a_close_request` | **回归防线**：主窗口可见 + 弹窗，50 次轮询不触发 |
| `test_busy_dialog_without_main_window_requires_consecutive_polls` | 前 4 次不触发，第 5 次触发 |
| `test_busy_dialog_counter_resets_when_the_dialog_clears` | 计数在弹窗消失后归零，不会累积误触发 |
| `test_missing_session_resets_visible_state` | 原有行为不变（pid 消失重置） |

全项目只有 `pyaedt_worker.py:73–77` 一处实例化 `AedtCloseWatcher`，
`on_close_intent` 只有 `handle_close_intent` 一个消费者 → **影响面 2 个文件**。

---

## 6. 改动 C（2026-10-21 晚）：`analyze_design` 不再隐式保存工程

### 6.1 症状

在 32 GB 机器上，任何"新建设计 + 通过 MCP 提交求解"的流程都会失败：

- 求解 2–4 分钟后 `hf3d.exe` 内存冲到约 19 GB 然后消失
- 结果目录只留下几十 MB 的残片，消息窗口出现 `SOLVER_OUT_OF_MEMORY`
- 用 `run_python_code` 手动确认 setup 的 `Solver Type` 是 `Iterative Solver`，
  但一旦通过 MCP 的 `analyze_design` 提交，它就变回 `Direct Solver`
- PyAEDT 日志里，"Solving design setup" 之前总有一行 `Project Saved correctly`

### 6.2 根因链

1. `mcp_server.py::analyze_design` → `pyaedt_capabilities.py::_analyze_design`
2. 后者调用 PyAEDT 的 `app.analyze(...)`
3. PyAEDT 的 `analyze()` **第一行就是** `self.save_project()`
   （`ansys/aedt/core/application/analysis.py:1816`，**无条件**执行，没有开关）
4. ~~AEDT 2026 R1 在保存工程时会把 `Solver Type` 重置为 `Direct Solver`~~
   **这条已实测推翻（2026-10-22）**：保存后、乃至换新会话后，`Iterative Solver` 都保持不变。
   真实原因是 `create_setup()` 建出来的 setup **默认就是 `Direct Solver`**，
   而当时那次写入落在**劣化会话**上、静默失效了。见 `AEDT_AI_PLAYBOOK.md` §4.1/§4.3
5. Direct 求解器对 70 mm 口径 / 28 GHz 的模型需要约 19 GB，超出本机可用内存

**历史更正**：旧的“保存后无法改回、只能复制Iterative设计”推断不成立。本项目后续通过原生setup属性 `UseIterativeSolver=True` 配置，并以原生 `Solver Type` 读回确认；不要依赖缓存别名。不同求解器应分别验证。

### 6.3 改动内容

`pyaedt_capabilities.py::_analyze_design`：

| 模式 | 改动前 | 改动后 |
| --- | --- | --- |
| 交互（`solve_in_batch=False`，默认）| `app.analyze(...)` → 隐式保存 ✗ | 直接调 `app.analyze_setup(...)` → **不保存** ✅ |
| 批量（`solve_in_batch=True`）| `app.analyze(...)` | 保持 `app.analyze(...)` ✅ |

- 选择 `analyze_setup` 的依据：已核实其函数体内**没有任何** `save_project()`
  （`analysis.py:1994`），且签名完整支持 `cores / tasks / gpus / acf_file /
  use_auto_settings / revert_to_initial_mesh / blocking`，资源控制能力无损失。
- **批量模式必须保留 `analyze()`**：批量求解需要保存并关闭工程再重开，保存是它的前提。
- 新增参数 **`save_before_solve`（默认 `False`）**：需要旧行为（提交前落盘）时显式打开，
  等价于改动前的行为。

`mcp_server.py::analyze_design` 增加同名参数并透传，docstring 说明原因。

### 6.4 为什么不在 PyAEDT 里改

`analysis.py:1816` 的 `self.save_project()` 在第三方包内。改它会带来三个问题：

1. 每次升级 PyAEDT 都要重打补丁；
2. 会影响所有调用 `analyze()` 的路径，包括 `solve_in_batch` 依赖的前置保存；
3. 无法用本仓库的 git 回滚。

在 MCP 层绕开更安全、可回滚、可测试。

### 6.5 测试

新增/更新 `tests/test_official_capabilities.py`：

| 用例 | 断言 |
| --- | --- |
| `test_interactive_analysis_does_not_save_the_project` | 交互模式下 `save_project` **一次都没被调用**，且走的是 `analyze_setup` |
| `test_save_before_solve_is_opt_in` | 显式传 `save_before_solve=True` 时才保存一次 |
| `test_batch_solve_keeps_the_pyaedt_analyze_path` | 批量模式仍走 `analyze()`，且 `solve_in_batch=True` |
| `test_validate_then_analyze_is_async_and_does_not_claim_completion` | 改断言 `analyze_setup_calls`，并新增 `analyze_calls == []` |
| `test_icepak_safe_mode_can_be_disabled` | 同上，断言资源参数仍被正确下发 |

为配合测试，`FakeApp` 增加了 `analyze_setup()`、`save_project()` 与两个记录列表。

**结果**：

```
python -m unittest tests.test_official_capabilities   ->  Ran 13 tests  OK
python -m unittest discover -s tests -p "test_*.py"   ->  Ran 146 tests  FAILED (failures=1, skipped=1)
```

那 1 个失败在 `tests/test_aedt_launcher.py`，**在改动前的基线上同样失败**
（已用 `git stash` 单独验证：11 tests, 1 failure）。原因是它假定本机没装 AEDT，
而本机装了 2026.1。**与改动 C 无关。**

### 6.6 回滚

```powershell
cd D:\Application\aedt-mcp
$git = "D:\Application\Git\cmd\git.exe"
& $git checkout -- pyaedt_capabilities.py mcp_server.py tests/test_official_capabilities.py
```

若已提交，用 `& $git revert <提交号>`。改动需**重启 MCP 服务**才生效（broker 是独立进程）。

### 6.7 遗留问题（本改动没有解决）

改动C不会自动选择求解器。HFSS实测应设置`UseIterativeSolver=True`并读取原生`Solver Type`；`DrivenSolverType`别名曾被静默丢弃。不要把复制设计作为默认绕行，也不要认为不保存就保证求解器正确。不同版本、求解类型分别验证；保存与结果保护按用户授权和备份策略进行。


## 7. 验证证据

### 6.1 单元测试

清掉 `AEDT_INSTALL_DIR` 后（否则会有 `LOCAL_SETUP.md` 第 7.1 节坑 4 记录的假失败）：

```powershell
cd D:\Application\aedt-mcp
Remove-Item Env:AEDT_INSTALL_DIR -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

结果：**Ran 143 tests — OK (skipped=1)**（改造前 141 项，3 个 watcher 用例扩为 5 个）。

### 6.2 端到端长调用实测（A 的关键验证）

用一个全新非图形 AEDT 会话，执行 **90 秒**的 `run_python_code`：

```json
{
  "executed": true,
  "result": {
    "slept_s": 90.0,
    "aedt_version": "2026.1",
    "broker_port": 62727,
    "verdict": "long call survived past the old 60 s cap"
  },
  "stdout": "long call completed after 90.0 seconds\n"
}
```

**判据**：在旧配置（60 s）下，这个调用必然抛
`AEDT broker timed out after 60s` 并杀掉 broker。实际完整跑完 90 s 且 broker 存活，
证明新预算**确实**作用到了服务端进程（也顺带证明 yml 的 `env` 块被正确传递）。

### 6.3 配置解析校验

用 PyYAML 解析 `cordis.patch.yml` 后确认：

- 顶层 2 个 insert 条目
- `[ansys-aedt] toolCallTimeoutMs=2400000`，`AEDT_WORKER_TIMEOUT='1800'`，`AEDT_LAUNCH_TIMEOUT='240'`
- `[matlab] toolCallTimeoutMs=900000` ← **未被误改**

> ⚠️ 文件中 `toolCallTimeoutMs` 出现**两次**（AEDT 在第 60 行、MATLAB 在第 93 行）。
> 修改时只改 AEDT 那条，否则会顺带把 MATLAB 的预算翻倍。

### 6.4 改动范围核验

```text
 aedt_close_watcher.py            | 44 ++++++++++++++++------
 tests/test_aedt_close_watcher.py | 49 ++++++++++++++++++++---
 2 files changed, 83 insertions(+), 10 deletions(-)
```

只有这两个文件被改。DSH 配置不在 git 内，已单独备份（见第 8 节）。

---

## 8. 回滚

### 7.1 A（配置）

```powershell
# 方式一：用备份覆盖（备份是改动前的逐字节副本）
Copy-Item -LiteralPath "C:\Users\lingze\.dsh\cordis.patch.yml.bak-20261021-before-A" `
          -Destination "C:\Users\lingze\.dsh\cordis.patch.yml" -Force

# 方式二：手工改回三个值
#   AEDT_WORKER_TIMEOUT: '1800' -> '60'
#   AEDT_LAUNCH_TIMEOUT: '240'  -> '120'
#   toolCallTimeoutMs: 2400000  -> 900000     （注意只改 AEDT 那条）

# 用户级环境变量同步回旧值（可选）
[Environment]::SetEnvironmentVariable("AEDT_WORKER_TIMEOUT", "60",  "User")
[Environment]::SetEnvironmentVariable("AEDT_LAUNCH_TIMEOUT", "120", "User")
```

备份文件 SHA256（改动前）：`8C4F669AA7C5955154E2DDD41E115E7F094E231969106FB8889DC5B0943B8E9F`

该文件被 Cordis HMR 监听，**保存即生效，无需重启 DSH**。

### 7.2 B1（代码）

```powershell
cd D:\Application\aedt-mcp
$git = "D:\Application\Git\cmd\git.exe"

& $git checkout -- aedt_close_watcher.py tests/test_aedt_close_watcher.py
```

然后重启 MCP 服务（broker 是独立进程，改动需重新加载）：

- 改一下 `cordis.patch.yml`（哪怕加个空格）触发 HMR；或
- 重启 DeepSeek Harness。

**若已提交**，用基线或指定提交退回：

```powershell
& $git log --oneline                 # 找到 A+B1 的提交号
& $git revert <提交号>               # 保留历史，推荐
# 或
& $git reset --hard f6173fc          # 直接回到最初的 baseline
```

**已验证回滚的字节保真性**：本仓库设了 `core.autocrlf=false` + `core.eol=lf`，
做了「改文件 → `git checkout --` → SHA256 比对」的往返测试，结果完全一致
（详见 `LOCAL_SETUP.md` 第 10.2 节）。

---

## 9. 仍然存在的问题（本次未解决）

| 问题 | 状态 | 说明 |
| --- | --- | --- |
| AEDT 会话跑 6–10 小时后自身劣化 | **未解决，属 AEDT 自身** | 手册 2.3。只能靠主动轮换会话（建议 4 小时或首次 `GrpcApiError` 即重启） |
| 求解期间并发操作会打断求解 | **未解决，属使用方式** | 手册 2.5。A 只是让"等待"不再被杀，不阻止你并发操作 |
| 超时后 broker 仍需 5 秒宽限才被杀 | **未改（即原计划的 B2）** | 现在超时后 `_stop_broker(graceful_timeout=0.5)` 立刻动手。B2 会改成先软等 30–60 s 再杀，风险是把"真卡死"藏得更深，故本轮不做 |
| 同一 AEDT 可能被 pid/port 两个 key 各挂一个 broker | **未改（B3）** | 上游有别名登记机制部分缓解，未做统一别名表 |
| `analyze_design` 提交前会保存工程 | **已由改动 C 解决** | 交互模式改走 `analyze_setup`，不再保存。批量模式仍需保存（其固有前提）|

---

## 10. 与上游的差异总表（累积）

重新下载上游文件后，以下改动**全部需要按文档重新施加**：

| # | 文件 | 差异 | 原因 | 文档 |
| --- | --- | --- | --- | --- |
| 1 | `pyproject.toml` | `mcp>=1.0` → `mcp>=1.0,<2` | MCP SDK 2.x 删除了 `mcp.server.fastmcp` | LOCAL_SETUP 第 3 节 |
| 2 | `pyproject.toml` | `pyaedt==1.5.0` → `==1.9.0` | 用户主动升级，已验证结果等价 | LOCAL_SETUP 第 3.1 节 |
| 3 | `.gitignore` | 增加 `.venv/`、`__pycache__/`、`*.egg-info/`、`batch.log`、`screenshot.jpg` | 本地 git 仓库需要 | LOCAL_SETUP 第 10.2 节 |
| 4 | `aedt_close_watcher.py` | busy 弹窗判定收紧 | **本文 B1** | 本文第 5 节 |
| 5 | `tests/test_aedt_close_watcher.py` | 3 → 5 个用例 | 配合 B1 | 本文第 5 节 |
| 6 | `LOCAL_SETUP.md` | 新增本地运维记录 | 本地文档 | —— |
| 7 | `STABILITY_IMPROVEMENTS.md` | 新增（本文） | 本地文档 | —— |
| 8 | `pyaedt_capabilities.py` | `_analyze_design` 交互模式改调 `analyze_setup`；新增 `save_before_solve` | **本文 C** | 本文第 6 节 |
| 9 | `mcp_server.py` | `analyze_design` 新增 `save_before_solve` 参数 + docstring | 配合 C | 本文第 6 节 |
| 10 | `tests/test_official_capabilities.py` | `FakeApp` 增 `analyze_setup`/`save_project`；新增 3 个用例、更新 2 个 | 配合 C | 本文第 6 节 |

DSH 侧（不在本仓库）：`C:\Users\lingze\.dsh\cordis.patch.yml` 中 AEDT 条目的
三个超时值（见第 4 节）。

---

## 11. 一句话总结

> 长调用之所以看起来像「gRPC 不稳定」，是因为超时会在操作中途杀掉 PyAEDT 客户端，
> 进而破坏 AEDT 2026 R1 的 gRPC 会话；而第二个客户端连上来时出现的「被占用」弹窗，
> 又被 close watcher 当成用户关闭请求，直接 `QuitApplication()` 把 AEDT 杀掉。
> A 让超时不再轻易发生，B1 让「发生超时」不再升级成「AEDT 被杀」。
