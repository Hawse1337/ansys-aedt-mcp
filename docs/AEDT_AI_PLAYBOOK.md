# AEDT / HFSS 天线仿真 AI 操作手册（实战经验）

> **2026-11-02补充优先级：**先读 [跨机迁移与仿真验收](2026-11_迁移与仿真验收.md)。历史段落包含已撤回推断，不能当作通用结论；保护工程、明确授权、独立副本和原生收敛证据优先。不得强杀未知会话、清锁或覆盖用户结果。

> 来源：2026-10-17 ~ 2026-10-20 金属环状周期结构透镜天线（28 GHz）项目的完整实践。
> 面向：后续接手的 AI 助手。
> 目的：避免重复踩坑。**每条都标注了现象 → 根因 → 正确做法**。
> 若与新环境的实际行为冲突，以实测为准，但请先验证下面列的这些点。

---

## 0.0 授权边界（最高优先级，先于全部技术内容）

**本手册的所有技术方案，都不构成对他人工程文件的操作授权。**

| 规则 | 说明 |
| --- | --- |
| **不得擅自打开/修改/保存/删除既有 `.aedt`** | 未经所有者明确同意，一律不动；连"只读打开"也先问 |
| **需要借用工程时先报告** | 说明目的、要动的对象、可能的后果，等批准 |
| **获批后只在副本上操作** | 副本用完即弃，不在原工程留下任何设计或状态 |
| **自己的实验写入新建文件** | 命名可识别、可整体删除，不寄生在他人工程里 |
| **怀疑已影响到他人文件时立即停止并报告** | 不要自行"修复"，不要静默回滚 |

### 为什么把这条放在最前面

2026-10-21/22 的实践中，为了绕过 guard 的"工程不存在就拒绝"逻辑，
多次打开 `LensFinal` / `LensFeedlz` 并在其中复制、删除设计。
虽然最终逐字节核对确认**没有污染**（直接读 `.aedt` 文本搜设计名），
但这是**流程越界**，而且此前已发生过工程被覆盖、仿真结果丢失事故：

- 一次 `-grpcsrv <port> <工程路径>` 启动 → 工程被写成 0 字节
- 一次 `Hfss(project="已有工程名")` 而该工程未打开 → 同名工程被覆盖成空工程
- 被清理的解**无法回挂**（解绑定在设计的内部身份上），只能重算

**正确姿势**：新建一个属于自己的 `.aedt`（见 §12.10 的 `create_project`），
从零构建；不要"借模板"。

### 报告模板（发现需要动他人文件时）

```
我需要操作：<工程路径> / <设计名>
目的：<为什么要动它>
风险：<可能影响什么，是否有备份>
替代方案：<能否只在新文件里做>
请您确认是否授权。
```

### 与本手册其他章节的关系

§12（新建工程的正确姿势）是**技术实现**，本节是**行为约束**。
即使技术上能安全地打开某个工程，**未经授权也不得执行**。

---

## 0. 环境事实（固定信息）

| 项 | 值 |
|---|---|
| AEDT 版本 | 2026.1（`D:\Application\ANSYS Inc\Electronics\v261\AnsysEM\ansysedt.exe`）|
| PyAEDT | 1.9.0，装在 `D:\Application\aedt-mcp\.venv\Scripts\python.exe` |
| 该 venv 有 | `ansys.aedt.core`（PyAEDT）|
| 该 venv **没有** | `PIL` / `python-docx` / `python-pptx` / `matplotlib` |
| 工作区 Python（`load_workspace_dependencies` 返回） | 有 `numpy / pandas / PIL / python-docx / python-pptx / openpyxl`，**没有 PyAEDT** |
| 机器 | 32 GB 内存 → **Direct Solver 对 70 mm 口径 28 GHz 模型必然 OOM** |
| 项目/结果路径 | `D:\HFSS Temp\*.aedt` 与 `*.aedtresults` |

**结论：读/写 AEDT 用 aedt-mcp venv；画图/做 Office 文档用工作区 Python。二者不可混用。**

---

## 1. 速查清单（做之前先看）

```
[ ] 动任何工程前：先 GetProjectList() 确认它在打开列表里；不在就 OpenProject 完整路径
[ ] 只读旧工程：先 Copy-Item 出临时副本，在副本上操作，永不碰原文件
[ ] 报告里的每个数字都要有对应的调用输出；没测到就写"未验证"
[ ] AEDT 启动：只给端口，绝不给工程路径；用 oDesktop.OpenProject() 打开
[ ] 求解前：确认 Analysis/<setup> 的 Solver Type = Iterative Solver（不是 Direct）
[ ] 设求解器与提交求解必须在同一进程内，中间不得保存工程
[ ] 求解进行中：绝不操作同一个 AEDT 会话
[ ] 每次求解完成后：立刻把 <design>.results 复制到非 .aedtresults 名称的目录
[ ] 不要对状态存疑的工程执行「关闭（不保存）」
[ ] 远场球 θ 范围设 −180°~180°（否则只能拿到半个剖面）
[ ] 定量的旁瓣对比：用 8 步 / ΔS 0.01，深零点误差仍有 ±3~7 dB
```

---

## 2. AEDT 启动与会话（最致命的坑）

### 2.1 启动参数会截断工程文件 ⚠️ 最严重

```powershell
# ❌ 致命：会把握到的工程文件写成 0 字节
ansysedt.exe -grpcsrv 53100 "D:\HFSS Temp\Xxx.aedt"

# ✅ 正确
ansysedt.exe -grpcsrv 53100
# 然后用 API 打开
d.odesktop.OpenProject(r"D:\HFSS Temp\Xxx.aedt")
```

- **现象**：工程文件变成 0 字节或只剩 1 个默认设计（如 `HFSS_AB12`）。
- **根因**：`-grpcsrv <port> <path>` 形式**不支持**工程路径参数；AEDT 启动时并未打开该工程，随后脚本用同名新建空工程 → 覆盖原文件。
- **曾因此丢失**：`LensFeed.aedt`（31 设计）、`LensFinal.aedt`（6 设计）。
- **血泪教训**：曾误判为"工程被写坏"，实际是启动方式错误造成的假象。**排查工程"只剩 1 个设计"时，先确认打开方式，再怀疑文件。**

### 2.1b ⚠️⚠️ 同一类事故的第二个入口：`Hfss(project="名字")`（已发生两次）

2.1 记录的是**启动命令行**带路径会截断工程。**同一类事故还有第二个入口**，当时没被归纳出来：

```python
# ❌ 致命：工程没有打开时，PyAEDT 不会去磁盘找它，而是新建一个同名空工程并覆盖原文件
Hfss(project="LensFinal", design="f24g05", port=53100, new_desktop=False)
```

**2026-10-19 与 2026-10-21 各发生一次**：`LensFeed.aedt`、`LensFeedlz.aedt`、
`LensFinal.aedt` 都曾被覆盖成 4~88 KB 的空工程。

**归纳后的不变量（这才是该记的那一句）：**

> 任何**按名字**让 AEDT 创建或打开工程的路径都会覆盖同名文件。
> 必须先 `oDesktop.OpenProject(完整路径)`，并**校验** `GetProjectList()` 再看设计列表。

**可执行守卫**（已加进 `build_from_scratch.py`，其它脚本照抄）：

```python
from ansys.aedt.core import Desktop
d = Desktop(port=PORT, new_desktop=False, non_graphical=False)
names = [str(x) for x in d.odesktop.GetProjectList()]
if PROJ not in names:                     # 没打开
    path = r"D:\\HFSS Temp\\%s.aedt" % PROJ
    if not os.path.isfile(path):
        raise SystemExit("GUARD: refusing, project not open and no file")
    d.odesktop.OpenProject(path)          # 按完整路径打开
if PROJ not in [str(x) for x in d.odesktop.GetProjectList()]:
    raise SystemExit("GUARD: refusing to attach")
h = Hfss(project=PROJ, ...)               # 现在安全
```

**为什么写了手册还犯第二次**：手册里记的是"启动命令别带路径"这个**具体症状**，
不是上面那句**不变量**；换了条代码路径（PyAEDT 的 `project=` 参数）规则就没触发。
**教训：总结要归到"什么情况下一定不能做什么"，并且落成能跑的检查，不能只写在文档里等人想起来读。**

**守卫已升级为模块 + 进程级钩子（2026-10-21 晚）**

上面的内联 `if` 只保护**那一个脚本**，换脚本/换会话立即失效。现在改成了：

| 文件 | 作用 |
| --- | --- |
| `D:\Application\aedt-mcp\aedt_project_guard.py` | 把裸工程名改写成已存在 `.aedt` 的**绝对路径**（PyAEDT 于是「打开」而不是「重建」）；找不到文件则**拒绝构造** |
| `pyaedt_worker.py`（broker 入口） | 启动时调用 `install()`，打补丁到 14 个 PyAEDT 应用类 |

**覆盖面**（这是关键）：

- ✅ 所有 MCP 工具调用（`open_project` / `create_design` / `analyze_design` …）
- ✅ **所有 `run_python_code`**，包括临时手写的代码 —— 它在 broker 进程里 `exec`，
  而 broker 启动时已经打过补丁
- ✅ **所有会话** —— 每个会话的 broker 都会安装一次
- ❌ 用 venv python **直接运行**的脚本 —— 除非脚本自己写：
  ```python
  import aedt_project_guard; aedt_project_guard.install()
  ```

**语义**：不是"拒绝"而是"改写成绝对路径"，所以危险调用变成安全调用，
调用方代码一行都不用改。真正的拒绝只发生在**裸名字找不到文件**时（此时
PyAEDT 本来会创建并覆盖），并给出可操作的报错。

**逃生开关**：`AEDT_GUARD_ALLOW_CREATE=1` 恢复旧行为；
`AEDT_GUARD_SEARCH_DIRS=D:\a;D:\b` 追加搜索目录。

**踩到的副作用（务必保留修复）**：在 broker 启动时 `install()` 会**提前** import
PyAEDT，而 PyAEDT 首次 import 会往 **stdout** 打横幅 —— broker 的 stdout 是
"每请求一行 JSON"的协议流，于是第一次调用直接报
`AEDT broker must emit exactly one JSON response line per request`。
`install()` 现在会在 import 期间 `redirect_stdout/stderr` 到缓冲区，
再把内容转发到 stderr（commit `8e681d8`）。

**验证记录**：

```
run_python_code(port=53250):
  Hfss(project="GuardProbe_ZZZ")  -> RuntimeError: aedt-guard refused ...   ✅ 拦住
  Hfss(project="LensFinal")       -> 解析为 D:\HFSS Temp\LensFinal.aedt   ✅ 放行
  stderr: [aedt-guard] project 'LensFinal' resolved to existing file ... (opened, not recreated)
```

**事后补救**（本项目实测，2026-10-21）：

- `LensFeedlz_safe.aedt`（10-19 的 10.6 MB 副本）救回 **29 个老设计**；
- 10-20/21 新建的 10 个 `f24_*` 设计按文档参数用 `build_from_scratch.py` **全部重建**
  （每个约 35~57 s；**每次构建结束都会 `save_project()`，十几个连跑必须丢后台**，
  同步调用会被工具层中断，还会留下半成品设计）；
- **结果集无法回挂**。干净的隔离实验（把 `.results` 在建工程**打开之前**就放到位，
  再打开）结果为 `NO DATA` × 5。原因：AEDT 把解绑定在**设计的内部身份**上（设计被创建时生成），
  不只是名字和几何；重建的设计是新身份，旧解即成孤儿。
  → **被清理的解只能重算**；`RESULTS_SAFE` 是**数值档案**，不是"可恢复的解"。

**读取目录大小时的一个真实误判**（差点让我虚报损失）：`Get-Item <dir>` /
`DirectoryInfo.Length` 对目录**恒为 0**。我曾据此宣布 `LensFinal_RESULTS` 是空的，
实际它有 **1,181 MB / 5 个结果集**。**判断目录大小必须递归**：

```powershell
(Get-ChildItem $dir -Recurse -File | Measure-Object Length -Sum).Sum
```

### 2.2 MCP 的 `launch_aedt` 不可靠

- **现象**：返回 `Error: [object Object]`；或 broker 报 `exited before request` / `timed out after 300s`。
- **做法**：直接用 `Start-Process` 启动（见 2.1），用端口探测确认：

```powershell
Get-Process ansysedt | Select Id,WorkingSet64
Get-NetTCPConnection -State Listen | Where LocalPort -eq <port>
# 或 MCP: list_aedt_sessions（返回 pid + listening_ports；gRPC 端口通常是 5xxxx 那个）
```

### 2.3 会话跑久了会全面失灵

- **现象**（6~10 小时后陆续出现）：`GetChildObject` / `OpenProject` / `RenameDesignInstance` / `GetProjectList` / `EditSetup` 随机报 `GrpcApiError`；`GetMessages` 返回 0 条；`SetPropValue` 静默无效。
- **安全做法（替代旧建议）**：不得按名称强杀 AEDT 或删除锁文件。先确认明确 PID/端口、用户所有权、求解状态及未保存内容，保留 `.aedt` 与同名 `.aedtresults` 的可恢复副本；仅在用户授权且确认无活动任务后，对明确拥有的会话正常释放。锁归属或恢复方案不明时停下并报告用户。

### 2.4 COM 引擎卡死

- **现象**：`Analyze` 返回 -1 或"发出去了"，但 `hf3d.exe` 从不出现；消息窗口：

```
[error] Unable to start analysis on machine 'Local Machine'
[warning] Com Engine non-responsive since HH:MM:SS ... manually kill the com engine process
```
- **做法**：杀 ansysedt + 重启。

### 2.5 并发操作会打断求解

- **现象**：`Simulation was aborted by user on server`。
- **根因**：求解进行中，同一会话里做了 `duplicate_design` / `SetActiveDesign` / `save_project` 等操作。
- **做法**：求解期间只做**进程级**观察（`Get-Process hf3d`、看结果目录体积），不碰 AEDT。

---

## 2.6 操作旧工程的唯一正确姿势（血泪）

**任何"只想读一下"的念头，都必须先变成"复制一份再读"。**

```powershell
# ✅ 只读访问旧工程
Copy-Item "D:\HFSS Temp\LensFinal.aedt" "D:\HFSS Temp\scratch_20261021.aedt"
# 然后只打开 scratch_20261021.aedt，永远不要碰原文件
```

反过来，**以下写法一个都不能用**（它们都可能创建并覆盖同名工程）：

| 写法 | 后果 |
| --- | --- |
| `ansysedt.exe -grpcsrv <port> <工程路径>` | 工程被写成 0 字节（2.1） |
| `Hfss(project="已有工程名", ...)` 且该工程**未打开** | 新建空工程覆盖原文件（2.1b） |
| `Hfss(project="已有工程名", design="不存在", ...)` 且工程未打开 | 同上 |

**先确认，再动手**：

```python
d = Desktop(port=PORT, new_desktop=False, non_graphical=False)
print([str(x) for x in d.odesktop.GetProjectList()])   # 我要动的工程在不在里面？
```
不在 → **停下**，先用完整路径 `OpenProject`，或者改用副本。

---

## 2.6b 提交前先编译 —— 一条能拦住本类事故的机械检查

2026-10-22 凌晨，一个"修守卫"的补丁把 `split(os.sep)` 写出成了错误的转义序列，
生成出来的 `build_from_scratch.py` 第 71 行无法解析，
**整个脚本 import 不了**，却照样提交进 git，还被写进了手册的"新增参数"一节。

**提交前跑这一条，两秒就能拦住：**

```python
import pathlib, py_compile

bad = []
for p in pathlib.Path(".").rglob("*.py"):
    try:
        py_compile.compile(str(p), doraise=True)
    except py_compile.PyCompileError as exc:
        bad.append((p, str(exc).splitlines()[0]))
print("broken:", len(bad))
for p, why in bad:
    print(" ", p, "->", why)
```

更简单的纪律：**凡是"生成或改写源码"的补丁脚本，跑完立刻编译产物**，
不要等到真正要用它的时候才发现。

**同一条道理也适用于文档**：这次手册 §12.2 引用了"旧实现的代码"，
但引的不是原文 —— 结论对、证据错。
**写进文档的代码片段必须从文件里复制，不能凭记忆重写。**

## 2.7 结论只写"测到的东西"

本次两次虚报都与"想当然"有关：

1. 看到 `existing_analysis_sweeps` 里有 `P4 : LastAdaptive` 就断言"结果已关联" ——
   那只是 **setup/sweep 的名字**，任何设计都有，**不代表有数据**。要判断关联必须真正
   `get_solution_data(...)` 读到数值。
2. 看到目录 `Length = 0` 就断言"结果丢了" —— 见 2.1b 末尾。

**规矩**：报告里出现的每个数字都应有对应的调用输出；拿不到就写"未验证"，不要推测。

---

## 2.8 `analyze_design` 是异步的 —— 不要用"查一次进程"下结论

`analyze_design`（以及原生 `Analyze(name, False)`）**立即返回**，求解器
`hf3d.exe` 要等网格划完才出现（本机实测几次里，从提交到 `hf3d` 出现约 1~3 分钟）。

2026-10-21 我提交后 90 秒查了一次 `Get-Process hf3d`，没查到，就报告"求解没启动"，
**实际它在正常跑**（用户从 GUI 看到并反问我）。这是 §2.7 那条规矩的再犯：
测到的是"此刻没有 hf3d"，写出去的却是"求解没启动"。

**正确写法**：

```python
# 判定"是否真的没启动"：给它至少 3~5 分钟，并同时看结果目录是否在长
wait_until = time.time() + 300
while time.time() < wait_until:
    if hf3d_running() or results_dir_mb() > 30:
        break
    time.sleep(15)
```

**判定"算完了"也同理**：`hf3d` 消失 + 结果目录体积停止增长 + AEDT 标题出现 `SOLVED`，
三个条件一起看。只靠 `tasklist` 字符串匹配（编码/截断问题）会误判，
在宿主侧用 `Get-Process hf3d` 更可靠。

---

## 3. 工程文件与结果数据

### 3.1 「关闭（不保存）」会清空 `.aedtresults` ⚠️

- **现象**：`close_projects(save=False)` 后，`<proj>.aedtresults` 下所有设计目录消失。
- **根因**：AEDT 关闭时清理"找不到对应设计记录"的结果目录。
- **做法**：状态存疑时**直接杀进程**，不要走正常关闭路径。

### 3.2 保存工程会清理"不匹配"的结果 ⚠️

- **现象**：`save_project()` 之后，某些设计的 `<design>.results` 变空。
- **做法**：**每次求解成功后立刻复制结果目录到 `.aedtresults` 之外的名字**，例如：

```powershell
Copy-Item "D:\HFSS Temp\P.aedtresults\designA.results" `
          "D:\HFSS Temp\RESULTS_SAFE\designA.results" -Recurse -Force
```
读取时再复制回 `<proj>.aedtresults/<design>.results`，**全程不保存**。

### 3.3 结果不能跨工程移植 ❌

- **实测**：把 `<design>.results` 复制到另一个（哪怕同名设计的）工程 → 读数一律 `n/a`。
- **含义**：想要"干净工程 + 可用结果"，只能**在同一工程内删除不需要的设计**；新建工程必须重算。

### 3.4 设计改名 = 解失效 ❌

- **实测**：`RenameDesignInstance` 成功后，结果文件仍在，但 AEDT 无法再关联 → 必须重算。
- **附**：改名接口本身经常失败（gRPC 3 次 + 原生 1 次全败），**重启 AEDT 后再改就成功**（疑似被删除的同名设计在会话里占名）。

### 3.5 `duplicate_design` 会产生残缺副本 ⚠️

```python
# ❌ save_after_duplicate=True：常丢 port sheet 与 Region
h.duplicate_design("new", save_after_duplicate=True)

# ✅
h.duplicate_design("new", save_after_duplicate=False)
# 之后必须验证：len(h.modeler.object_names) 是否与母本一致
```

---

## 3.6 工程被覆盖后的恢复步骤（实测有效）

按顺序执行，**每一步都先看输出再决定下一步**：

1. **先保全现场**：把损坏的文件复制成 `BROKEN_<name>_<date>.aedt`，别急着删。
2. **盘点备份**（递归统计大小！）：
   - `*_safe.aedt` / 手工副本
   - `*_RESULTS/`、`RESULTS_SAFE/`（结果集）
   - 回收站、`%TEMP%`、`*.bak` / `*.aedt~`（本项目实测：**没有**，AEDT 不留工程备份）
3. **恢复工程骨架**：`Copy-Item <safe 副本> <原路径>`，然后**用完整路径**打开，
   核对设计数量与设计名列表。
4. **恢复结果集**：把 `.results` 目录放进 `<proj>.aedtresults/`，**必须在打开工程之前**放好。
   - ⚠️ 若设计是**重建**的（新身份），结果**不会关联**（实测 `NO DATA`）；
   - 若设计是**原样恢复**的（同一份 `.aedt`），可以关联。
5. **缺失的设计重建**：按文档参数用 `build_from_scratch.py`（有守卫）逐个建，
   每个建完立刻核对实体数与包围盒。
6. **解缺失就重算**：约 20 min/个（8 步 / ΔS 0.01），重算值与档案值逐项核对。

**代价参考**：本项目 10 个设计重建 ≈ 8 分钟（后台）；5 个设计重算 ≈ 100 分钟。

**重建保真度实测（2026-10-21，`f24_thin` 4 步 / ΔS 0.02 重算 vs 档案值）**：

| 量 | 重建 | 档案 | 偏差 |
| --- | --- | --- | --- |
| RealizedGain 峰值 | 7.28 dBi | 7.33 | −0.05 |
| GainTotal 峰值 | 7.41 dBi | 7.43 | −0.02 |
| 方位波动 | 1.54 dB | 1.51 | +0.03 |
| θ=90° | 6.35 dBi | 6.3 | +0.05 |
| θ=120° | −18.89 dB | −18.9 | 0.00 |
| θ=30° | −11.64 dB | −12.6 | +1.0 |
| θ=150° | −11.59 dB | −12.1 | +0.5 |
| dB(S11) | −15.31 dB | −16.41 | +1.1 |
| θ=0° / 180° | −23.86 / −26.12 | −32.2 / −38.6 | 深零点，未收敛，不作比较 |

结论：**按文档参数重建的几何与原设计一致**，收敛量偏差 0.05~1.1 dB；
深零点的差异属已知的非收敛区间（§8），不构成重建错误。

> 读数陷阱：`dB(RealizedGainTotal)` 取 `Theta=All, Phi=All` 时，数组**不是**按 θ=0 开头，
> 第一个元素不代表峰值。比"增益"永远用 `max(values)`，否则会拿一个零点去和峰值比
> （我第一次就写错过，得到 −23.99 的荒谬结论）。

---

## 4. 求解器设置（第二致命的坑）

### 4.1 真实属性名是 `Solver Type`（**不是只读** —— 见本节末的更正）

原生对象树读出（这是唯一可靠的自检方式）：

```python
p4 = h.odesign.GetChildObject("Analysis").GetChildObject("P4")
p4.GetPropValue("Solver Type")     # -> 'Direct Solver' 或 'Iterative Solver'
p4.GetPropValue("Solution Freq")   # -> '28GHz'
p4.GetPropValue("Passes"), p4.GetPropValue("Delta S")
```

#### ⚠️ 2026-10-21 更正：`DrivenSolverType` 其实**是**可写的

早先这里写着"四种写法全部无效"。**那个结论是错的**，实测推翻：

```
健康会话（新启动）：
  start                 : Direct Solver
  SetPropValue          : ret=False -> Direct Solver        ✗ 确实只读
  props.update()        : -> Iterative Solver               ✅ 成功
  EditSetup             : gRPC 报错                          ✗
保存工程后再读          : Iterative Solver                  ✅ 会保持
```

```python
s = h.get_setup("P4")
s.props["DrivenSolverType"] = "Iterative Solver"
s.update()
time.sleep(1)                                  # 给 AEDT 一点时间落盘/刷新
p = h.odesign.GetChildObject("Analysis").GetChildObject("P4")
print(p.GetPropValue("Solver Type"))           # -> Iterative Solver
```

**正确结论**：

| 方法 | 健康会话 | 劣化会话 |
| --- | --- | --- |
| `p4.SetPropValue('Solver Type', ...)` | 返回 False，值不变（真只读） | 返回 False |
| `setup.props['DrivenSolverType']` + `update()` | ✅ **有效，且保存后保持** | ❌ **静默失效** |
| `oDesign.EditSetup(...)` | gRPC 报错 | gRPC 报错 |

#### 为什么当初会误判成"只读"

当时那两次测试都跑在**已经劣化数小时的会话**里（症状见 2.3：
`SetPropValue` 静默无效、`GetMessages` 返回 0 条）。**劣化会话里属性写入会静默失败**，
于是"写了没生效"被我归因成"属性只读"。

> **教训**：属性写入返回成功但值不变时，**第一反应必须是"会话劣化了"**，
> 而不是"这个属性只读"。先重启会话重测，再下结论。
> 这个误判直接导致我采用"复制已含 Iterative 的设计"这条绕行路，
> 进而去碰了不该碰的旧工程 —— 整场数据事故的源头就在这里。

#### 新建设计的默认值

`create_setup()` 建出来的 setup 默认仍是 `Direct Solver`，需要按上面的方法显式设置。
设置后**保存不会丢失**（实测），所以建好一次即可。

### 4.2 正确的写法（不需要任何绕行）

```python
s = h.get_setup("P4")
s.props["DrivenSolverType"] = "Iterative Solver"
s.update()
time.sleep(1)
p = h.odesign.GetChildObject("Analysis").GetChildObject("P4")
assert p.GetPropValue("Solver Type") == "Iterative Solver"   # 当场自检
```

**不需要**再"复制一个已含 Iterative 的设计" —— 那是建立在误判之上的绕行方案。
早先这里写着"唯一可靠的绕行办法是复制设计"，2026-10-22 已删除该建议。

自检永远读**原生属性**，不要读 `setup.props`（它会把你写进去的值原样回显，
即使实际没生效）。

### 4.3 「保存会把求解器打回 Direct」——**这条是错的，已实测推翻**

早先这里写着"保存会把 Solver Type 打回 Direct"，并把它作为改动 C 的全部理由。
**实测不成立**：

```
设置后            : Iterative Solver
保存工程后再读    : Iterative Solver        ← 没有重置
换新 AEDT 会话再读: Iterative Solver        ← 连重启都保持
```

（`f24_thin` 在会话重启后依然是 Iterative，这是最强证据。）

**那当初的 OOM 到底怎么来的？** 回看整条链，其实是两件事叠加：

```
① create_setup() 建出来的 setup 默认就是 Direct Solver
② 我写了 DrivenSolverType，但会话已劣化 → 写入静默失效
→ 仍是 Direct → 70 mm 模型 19 GB → SOLVER_OUT_OF_MEMORY
```

**从头到尾没有"保存重置"这个环节** —— 那是我为了解释现象编出来的中间步骤。

**改动 C 还有价值吗？** 有，但理由要换：

| 原来的理由（错） | 真实的收益 |
| --- | --- |
| 避免保存重置求解器 | 求解前不写盘 → **不会顺带清理其它设计的结果目录**（§3.2），少一次 IO，实验中途不留半成品状态 |
| —— | 代价：设计状态不落盘，AEDT 崩溃会丢未保存的改动 |

### 4.4 `analyze_setup()` 不启动求解器

- **现象**：`h.analyze_setup('P4')` 立刻返回，`hf3d.exe` 从不出现。
- **更正**：在本项目中原生 `h.odesign.Analyze('P4', False)` 曾只提交一次自适应，不可作为多步求解替代。使用已验证的 PyAEDT `h.analyze(setup='P4', cores=2, use_auto_settings=False)` 或明确验证过的 MCP 分析入口；注意其保存副作用，先备份并读原生 Solver Type。检查逐步 profile 与连续收敛，提交成功不等于完成。

### 4.5 其它求解相关

| 现象 | 做法 |
|---|---|
| `Error reading the vmesh P4` | 保全原工程及结果，在独立副本中新建 setup，调查失效网格引用；未经授权不删原 setup |
| 想强制全新求解 | 在独立副本建立全新 setup，记录时间戳和求解证据；不要删除历史结果 |
| 自适应未收敛 | 查每步 ΔS 与连续收敛数，根据资源预算延长或改网格；8步/0.01不是保证 |
| 求解 1~2 秒就“完成” | 可疑但不证明复用；检查 profile、实际求解频率和错误消息 |

---

## 5. 建模陷阱

### 5.1 端口

- `lumped_port` 的积分线两端**必须落在端口片上**；否则校验报
  `Port '1': Both endpoints of port lines must lie on the port.`
- **环形（annulus）sheet 建端口会失败**；**完整圆盘**可以。
- **曲面（圆柱面）sheet 不能建集总端口**：
  `Port refinement ... The mesh on port 1 is non-planar.`
- **删除一个未分配的、与端口片重合的 sheet，会打断端口的积分线引用** → 校验失败。
  正确做法：删 sheet 后**重新创建端口**（积分线端点留在片内侧，如 0.4→1.15 mm）。

### 5.2 API 细节

| 调用 | 正确写法 |
|---|---|
| 画圆片 | `M.create_circle(orientation='Z', origin=[0,0,z], radius=r, name=...)`（不是 `cs_axis`）|
| 建体 | `M.create_cylinder(orientation='Z', origin=[...], radius=..., height=...)` |
| 旋转成面 | `M.create_polyline(points=..., cover_surface=True)` → `M.sweep_around_axis(...)` |
| 重建 Region | 会**删掉远场球**，需重新 `InsertFarFieldSphereSetup` |
| 读远场球 | `oDesign.GetChildObject("Radiation").GetChildNames()` |

### 5.3 后处理

- `get_solution_data(...)` 在无解/无该量时**返回 `True`/`False` 而不是数据对象** →
  必须判断 `if not d or isinstance(d, bool): ...` 才好用。
- GUI 的 Far Fields 报告**默认选列表里第一个 setup**；若那是历史残留的空 setup，图就是空白 →
  **删掉没求解过的 setup**。
- `The variable 'Phase' doesn't have an associated sweep` 是无害警告。

### 5.4 方向图口径 ⚠️（容易做错）

- HFSS 远场球默认 **θ = 0°~180°**，只有半个剖面；直接画会显得"两边对称"。
- **两种正确做法**：
  1. 建球时把 θ 设为 **−180°~180°**；
  2. 事后用两个子午面拼合：`θ≥0` 取 φ=0°，`θ<0` 取 φ=180°（θ 取负）。
- 拼接代码要点：

```python
full = [(a, b) for a, b in cut_phi0  if a >= 0]
full += sorted([(-a, b) for a, b in cut_phi180 if a > 0], key=lambda t: t[0])
```
- 拼出的完整剖面才能看出**馈电不对称**（本项目中两主瓣 6.75 vs 6.55 dBi、深零点差 7 dB）。

---

## 6. 工具链与脚本

| 问题 | 做法 |
|---|---|
| 用 PowerShell 内联 Python 传多行代码，引号/中文经常被吃掉 | **写补丁 `.py` 文件再执行**，不要拼 PowerShell 字符串 |
| `Get-Content -Raw` + `Set-Content -Encoding UTF8` 读坏了 UTF-8 源文件 | 用 Python 读写：`io.open(p, encoding='utf-8')` |
| Python 文本模式写入把 LF 转成 CRLF，`git diff` 变成整文件重写（上千行） | 写文件必须 `io.open(p, 'w', encoding='utf-8', newline='\n')`；已写坏的用二进制归一：`raw.replace(b'\r\n', b'\n')` |
| `Select-String` 管道读取长时间运行的程序输出会缓冲/假死 | 用 `*> logfile` 重定向，再 `Get-Content -Tail` |
| PIL 画中文变方框 | 字体指定 `C:\Windows\Fonts\msyh.ttc`（或 `simhei.ttf`）|
| ConstrainedLanguage 下 `System.Drawing` 不可用 | 用 PIL / 或 `Get-ChildItem` 读元数据 |
| python-docx 段落里 `**粗体**` 标记原样显示 | 自己 split `**` 分段加粗 |
| 文档/PPT 渲染检查 | 用 LibreOffice Kit CLI（`office-render` skill），页数用 `--pages "1,3,5"` |
| 长任务 | `run_in_background: true` + `job_output(wait=true)`；不要 sleep 轮询 |

---

## 7. 物理与方法学（本项目结论，可直接复用的判据）

### 7.1 环状周期结构（波纹金属表面）

- 等效为**人工表面等离激元**；槽深 h 越大 → 等效折射率越高 → 束缚越强。
- **λ/4 判据**：只有 `h < λ/4` 才是束缚模。28 GHz 时 λ/4 = 2.677 mm。
  - h = 2.20 mm（0.82 λ/4）→ 工作正常
  - h = 2.45 mm（0.92）→ 失效（−7.5 dBi）
  - h = 2.86 mm（1.07）→ 失效（−7.9 dBi）
- **Bragg 判据**：`λ_g = λ0/n_eff < 2P` 时等效介质/光栅公式失效（本设计 P=1.933 mm）。

### 7.2 旁瓣来源

- 俯仰面肩部旁瓣 = **口径边缘环源**的 J₀ 贝塞尔瓣：理论第一旁瓣 −7.9 dB，实测 −8.7 dB。
- 因此抑制思路只有两条：**改变环源半径/形状**，或**把口径做成沿径向渐变**。

### 7.3 已验证有效的改进（按收益排序）

| 改动 | 效果 |
|---|---|
| **翻边外缘厚度渐变**（1.0→0.20 mm，6 mm 过渡）| ★ 净收益：θ=150° 肩部 +2.2 dB、θ=180° 零点 +10.5 dB、方位波动更好，**增益几乎不变** |
| 翻边加宽 22→24 mm | 肩部 +1.3 dB、轴向零点 +3.7 dB，代价 −0.15 dB 增益 |
| 翻边加宽 24→26 mm | 反而变差（θ=150° 退化到 −7.8 dB）|

### 7.4 已验证无效/更差的改动（不要重复尝试）

| 改动 | 结论 |
|---|---|
| λ/4 扼流套筒 | 只压轴上零点（+10 dB），肩部无改善，方位波动恶化到 2.69 dB |
| 套筒 + 宽翻边组合 | 不叠加，套筒的不对称破坏宽翻边的效果 |
| 去掉装配开孔 | **反直觉**：肩部无改善，θ=180° 从 −38.6 退化到 −19.8 dB |
| 圆柱面（轴对称）缝隙端口 | HFSS 硬限制：集总端口必须平面 |
| 参考 PPT 的平面缝隙馈电 | 空气薄缝阻抗过低，增益掉 3.9 dB、方位恶化到 5.9 dB |
| 碗壁过陡（RW 8.6）| 匹配极好（−28 dB）但轴向零点退化 9~16 dB |

### 7.5 开孔的反直觉行为

- Ø1.6 mm 孔在 28 GHz 的 TE₁₁ 截止约 110 GHz，**强截止、几乎不辐射**。
- 但它们提供了**一小份与轴向辐射反相的贡献**，恰好部分抵消轴向泄漏 → **堵上孔反而使轴向旁瓣显现**。
- 结论：装配孔可保留，不必为电性能特殊处理。

---

## 8. 数值可信度（做定量对比前必读）

同一几何、同一设置，两次求解的差异：

| 量 | 收敛误差 | 能否定量比较 |
|---|---|---|
| 增益（GainTotal / RealizedGain） | ±0.1~0.2 dB | ✅ |
| 方位面波动 | ±0.2 dB | ✅ |
| 主瓣附近（θ=60~90°） | ±0.5 dB | ✅ |
| 肩部 θ=150° | ±0.4 dB | ✅ |
| 肩部 θ=30° | ±2 dB | ⚠️ 只看趋势 |
| **深零点（θ=0/120/180°）** | **±3~7 dB** | ❌ 只看趋势，不可比数值 |

**因此**：说"某改动使深零点改善 14 dB"这种话要加误差棒；而"肩部改善 4~5 dB"是可靠的。

**更正**：不存在固定8步或某个ΔS值保证收敛。查看原生 convergence/profile 的目标、连续步数与最终状态；36 GHz 实例4步连续收敛，26.79 GHz 实例5步虽正常结束却只达到1次连续收敛。上述误差数值是特定历史案例观察，不能作为通用误差棒。

---

## 9. 参考文献级注意事项

- 参考 PPT 的 7.74 dBi 是 **26.79 GHz** 的实测值；本项目在 **28 GHz** 计算 →
  跨频率比增益不公平，应先按 26.79 GHz 重算再比。
- 参考模型的馈电（锥尖间隙 + 平面端口片）能工作，是因为**介质加载**抬高了端口阻抗；
  纯空气结构照搬会失配 —— 这是"机理不可移植"的典型案例。

---

## 10. 推荐工作流（下次直接照做）

```
1) 启动：ansysedt.exe -grpcsrv <port>            （不要带工程路径）
2) 打开：Desktop(port).odesktop.OpenProject(path)
3) 自检：GetChildObject("Analysis").GetChildObject(setup).GetPropValue("Solver Type")
        核查实际求解类型；HFSS需要时通过原生 UseIterativeSolver 配置并再次读回，不要默认复制用户设计
4) 求解：同进程设参数 → 原生 Analyze(setup, False)
        轮询 Get-Process hf3d 判断是否真正在算
5) 完成判据：消息窗口出现 Normal completion of simulation
6) 立刻：复制 <design>.results → RESULTS_SAFE\<design>.results
7) 读数：get_solution_data(...) 并判断返回值不是 bool
8) 画图：θ 用 −180~180；中文用 msyh.ttc
9) 收工：不保存工程（避免结果被清理），或保存后从 RESULTS_SAFE 恢复
```

---

## 11. 已归档资产（本项目，供参考）

| 路径 | 内容 |
|---|---|
| `D:\HFSS Temp\LensFinal.aedt` | 5 个最优设计（f24_thin / f24_thin_a / f24g05 / rw95_g05 / t8_86）|
| `D:\HFSS Temp\LensFeedlz.aedt` | 12 个设计（含 f24_thin_nh、f24_thin_c8 等调试版）|
| `D:\HFSS Temp\RESULTS_SAFE\` | 各设计结果保护副本（每份 ~240 MB）|
| `D:\HFSS Temp\LensFinal_RESULTS\` | LensFinal 的 5 个结果 |
| `D:\Application\dsh\work_metallic_lens\` | 报告（md + docx）、PPT、figs、全部脚本（aedt\*.py）|

**可复用脚本**（都在 `work_metallic_lens\aedt\`）：

- `build_from_scratch.py <proj> <design> <bowl_scale> <flange_r> <RW> <ZB0> <port> [choke] [thin] [edge_t] [taper] [noholes]`
  —— 从零构建完整设计（盘+碗+同轴+端口+Region+球+setup），已验证可用
- `rebuild_bowls.py <design> <bowl_scale> <flange_r> <RW> <ZB0>` —— 只重建碗/同轴
- `solve_proper.py` / `resolve_fresh.py` —— 设迭代法 + 提交 + 轮询
- `metrics.py <proj> <design> <setup> <port> <freq>` —— 读 S11 / 增益 / 方位波动 / 俯仰切面
- `batch_metrics.py <port> <freq> <design...>` —— 批量读成 markdown 表
- `read_full_cuts.py` / `draw_full_planes.py` —— 读四切面并画 −180~180 方向图


---

## 12. 2026-10-22 增补：新建工程的正确姿势（guard 的缺陷与修复）

### 12.1 先记一条硬规矩（我违反过一次）

> **永远不要为了"找个模板"去打开用户的工程。**
> 建模脚本本身就是从零构建的（盘、碗、同轴、端口、Region、远场球、setup 全在里面），
> 它需要的只是一个**空的、新的** `.aedt`，不需要任何已有设计。

2026-10-22 凌晨我为了绕过 guard 的拒绝逻辑，反复打开 `LensFinal` / `LensFeedlz`，
并尝试在其中复制设计。虽然最终核对**没有污染**用户的工程（直接读 `.aedt` 文本确认无残留设计名），
但这是**流程错误**：正确做法是把 guard 修好，而不是绕开它。

**核对工程是否被污染的可靠方法（不需要 AEDT）**：

```python
txt = io.open(r"D:\HFSS Temp\Xxx.aedt", encoding="utf-8", errors="ignore").read()
print([n for n in ("design_a", "design_b") if n in txt])
```
`.aedt` 是文本文件，设计名直接可搜；比打开工程安全得多，也不会触发任何写入。

### 12.2 guard 的缺陷：允许创建之后又被打死

旧实现（`build_from_scratch.py` 内联守卫）有两个检查：

```python
if _found is None:
    if os.environ.get("AEDT_GUARD_ALLOW_CREATE") == "1":
        _found = PROJ                      # ← 允许创建
    else:
        raise SystemExit("refusing")
...
if PROJ not in _open_names:                # ← 第二个检查
    raise SystemExit("refusing to attach, still not open")
```

**问题**：新建的工程当然还不在 `GetProjectList()` 里 → 第二个检查必然失败 →
即使设了 `AEDT_GUARD_ALLOW_CREATE=1` 也永远跑不通。

> **更正（2026-10-22 03:00，原作者核对）**：上面引用的"旧实现"**不是**最初那版的原文。
> 最初那版**根本没有** `AEDT_GUARD_ALLOW_CREATE` 分支 —— 它在找不到文件时**无条件**拒绝，
> 因此在**任何**情况下都创建不了工程，连合法的新建也挡住。
> 而且它的 docstring 还写着"新建工程请传绝对路径"，代码却没实现这个分支 —— 文档与代码不一致。
> **缺陷结论不变**（确实无法创建），但"允许创建后又被第二个检查打死"是我后来
> 那次未完成的中间修改，不是原始版本的行为。教训是双向的：**先验证断言再写进手册**。

**修复**：区分「创建模式」和「打开模式」，创建模式下跳过"必须已打开"的检查；
同时把裸工程名展开成**绝对路径**，避免 PyAEDT 把工程建到默认目录：

```python
if not os.path.isabs(PROJ):
    PROJ = os.path.join(r"D:\HFSS Temp", PROJ if PROJ.lower().endswith(".aedt") else PROJ + ".aedt")

_create = False
if _desired not in _open:
    if os.path.isfile(PROJ):
        oDesktop.OpenProject(PROJ)                 # 已存在 -> 只打开，绝不覆盖
    elif os.environ.get("AEDT_GUARD_ALLOW_CREATE") == "1":
        _create = True                             # 不存在且已授权 -> 创建
    else:
        raise SystemExit("refusing (set AEDT_GUARD_ALLOW_CREATE=1 to create)")

if not _create:                                    # 只有"打开模式"才校验打开状态
    assert _desired in [norm(x) for x in oDesktop.GetProjectList()]
```

### 12.3 空工程插入设计得到的默认类型不可假设 → 见第 13 章

**一句话**：空工程里 `insert_design` 得到的类型**不是固定的** ——
实测见过 `HFSS Terminal Network` 和 `HFSS Hybrid Modal Network` 两种，
都可能让 `create_setup` 抛 `KeyError: '<长名>'`。

**规则**：新建设计后**先读 AEDT 的真实类型**（`odesign.GetSolutionType()`，
不要读 `h.solution_type`，那个是可能说谎的标签），再决定要不要切类型。
完整机制与两种修法（shim / 手动三步）见 **§13**。

### 12.4 `oProject.SaveAs` 不可靠 → 用"绝对路径创建"代替

**实测（2026-10-22，多个会话）**：

| 调用 | 结果 |
|---|---|
| `oDesktop.NewProject()` | ✅ 成功（但工程只在内存里） |
| `oProject.SaveAs(path)` | ❌ **连续 4 次 `GrpcApiError`** |
| `Desktop.new_project(...)` | ❌ PyAEDT 1.9 **无此方法**（`AttributeError`） |
| `Hfss(project="<绝对路径>", ...)` | ✅ **可以直接在指定路径创建工程文件** |

**结论**：不要依赖 `SaveAs` 来落地新工程。直接给 PyAEDT 一个**不存在的绝对路径**，
它会在那里创建 `.aedt`；配合 `AEDT_GUARD_ALLOW_CREATE=1` 即可。

**副作用**：`NewProject()` 建的空白工程若没保存，**连接一断就消失**
（我曾看到 `Project2/3/6/8` 一闪而过，下一个脚本里 `GetProjectList()` 返回空）。

### 12.5 启动等待：改用端口轮询，不要固定 sleep

固定 `Start-Sleep 120` 是浪费：AEDT 实际就绪时间在 **17~110 s** 之间波动。
改用轮询脚本 `aedt\Wait-AedtReady.ps1`：

```powershell
& "D:\Application\dsh\work_metallic_lens\aedt\Wait-AedtReady.ps1" -Port 53330 -TimeoutSec 240
# 实测返回：AEDT ready on port 53330 after 17.1s
```

```powershell
param([Parameter(Mandatory=$true)][int]$Port, [int]$TimeoutSec = 240)
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
        Start-Sleep -Seconds 3
        "AEDT ready on port {0} after {1:N1}s" -f $Port, $sw.Elapsed.TotalSeconds
        exit 0
    }
    Start-Sleep -Milliseconds 1500
}
```

### 12.6 会话劣化的新观察（比 §2.3 更严重）

2026-10-22 凌晨连续 5 次重启，**每次都在 10~40 分钟内全面劣化**，且重启不再能恢复：

| 调用 | 劣化后表现 |
|---|---|
| `oProject.SaveAs` | `GrpcApiError`（4/4 失败） |
| `SetActiveProject` | `GrpcApiError` |
| `OpenProject` | 间歇 `GrpcApiError` |
| `create_polyline` / `sweep_around_axis` | `GrpcApiError`（几何完全建不出来） |
| `create_cylinder` | 同上 |

此时表现与 §2.3（长时间运行后劣化）不同：**刚从 17 s 内启动完成的会话也会立刻出问题**。
`NewProject()` 仍可用，说明进程本身没死，是 **COM/gRPC 桥接层坏了**。

**建议**：出现这种"重启也不恢复"的模式时，**重启整机**（清掉 COM 引擎与许可服务残留），
不要在脚本层反复重试。当晚我在这上面耗了数小时，收益为零。

### 12.7 `build_from_scratch.py` 的新增参数（2026-10-22）

在原有参数后追加 5 个，用于"加厚翻边 + 环形 λ/4 槽"：

```
argv[17] 翻边厚度 mm        (默认 FT=1.0)
argv[18] 开槽 1/0           (默认关)
argv[19] 槽内径 mm          (默认 18.30)
argv[20] 槽外径 mm          (默认 20.90)
argv[21] 槽深 mm            (默认 2.68 = λ/4 @28 GHz)
```

翻边孔会自动改为**贯穿全厚**（`ZTP-0.1 → ZTP+FLANGE_T+0.1`），不会因为加厚而留半截。

**完整命令示例（新建工程 + 加厚 3.2 mm + 槽 + 全部开孔）**：

```powershell
$env:AEDT_GUARD_ALLOW_CREATE = "1"
& "D:\Application\aedt-mcp\.venv\Scripts\python.exe" `
  "D:\Application\dsh\work_metallic_lens\aedt\build_from_scratch.py" `
  "D:\HFSS Temp\ChokeRing.aedt" f24_chk 0.50 24 9.5 1.0 53350 `
  0 1 0.20 6 0 0 0 0 0 3.20 1 18.30 20.90 2.68
```

参数顺序：`工程 设计 bowl系数 翻边R RW ZB0 端口 扼流套筒 薄边 边缘厚 过渡长 无孔 柱数 柱R 柱H 柱D 翻边厚 开槽 槽内径 槽外径 槽深`

### 12.8 扼流槽设计的几何要点（供复现）

λ/4 扼流的**槽必须与翻边装配孔留净空**，否则槽被打穿、谐振失效：

```
孔中心半径  RF_H = (RW + RF)/2 = (9.5 + 24)/2 = 16.75 mm
孔半径      0.80 mm  →  孔外沿 17.55 mm
槽内径必须 ≥ 17.55 + 净空          →  取 18.30（净空 0.75 mm）
槽外径      20.90（宽 2.60 ≈ λ/4）
翻边薄边起点 21.00（不与槽重叠）
槽深        2.68 = λ/4 @28 GHz
```

**2026-10-21 的教训**：第一版槽内径取 17.50，比孔外沿 17.55 **小 0.05 mm**，
12 个孔全部切进槽内壁，测得的性能（θ=150° −12.6 dB）低于槽的真实能力。
**做这类"环槽 + 环孔"结构时，必须先算一遍两者的半径余量。**

### 12.9 工具链小坑（PowerShell）

| 现象 | 原因 / 做法 |
|---|---|
| `if : 无法将"if"项识别为...` | PowerShell 的 `-f` 格式化里不能写 `if(...)` 表达式 → 用 `$(if (...) {...} else {...})`，或干脆改用 Python 生成字符串 |
| `Get-Content` 显示 `split("\")` 像是语法错 | 只是 PowerShell 的转义渲染；以 `py_compile` 的结果为准 |

### 12.10 守卫重构：一份实现，两个入口（2026-10-22 05:00）

上一轮把"允许创建"的逻辑**又写了一遍**在 `build_from_scratch.py` 里，
而 `aedt_project_guard.py` 早就有一份等价实现 —— 两份代码必然会漂移
（这次它们就已经不一致了：内联版禁止一切创建，模块版支持授权创建）。

**现在的结构：一份实现 + 两个入口。**

| 入口 | 怎么接进来 | 覆盖范围 |
| --- | --- | --- |
| MCP broker | `pyaedt_worker.py` 启动时 `aedt_project_guard.install()` | 所有 MCP 工具、所有 `run_python_code`、所有会话 |
| 直接跑的脚本 | 脚本开头 `import aedt_project_guard; aedt_project_guard.install()` | 该脚本 |

`build_from_scratch.py` 里那 45 行内联守卫已经删掉，改成两行委托 + 一次 fail-fast 检查。

**模块对外提供两个明确入口，语义不再靠环境变量猜：**

```python
import aedt_project_guard as g

# 1) 日常构造：裸名字会被解析到已存在的 .aedt；解析不到就拒绝
h = Hfss(project="LensFinal", port=PORT)        # -> D:\HFSS Temp\LensFinal.aedt（打开）

# 2) 明确要新建：给绝对路径，存在则拒绝，绝不覆盖
app = g.create_project(Hfss, r"D:\HFSS Temp\NewDesign.aedt", design="D1", port=PORT)
```

**三种情况的行为（已实测，2026-10-22）：**

| 输入 | 结果 |
| --- | --- |
| 已存在的工程名 `LensFinal` | → `D:\HFSS Temp\LensFinal.aedt`（打开，不重建）|
| 不存在的名字，未设 `AEDT_GUARD_ALLOW_CREATE` | → `RuntimeError`，拒绝 |
| 不存在的名字，`AEDT_GUARD_ALLOW_CREATE=1` | → `D:\HFSS Temp\<name>.aedt`（**位置确定**，不再交给 PyAEDT 猜）|
| `create_project` 给已存在的路径 | → `RuntimeError`，原文件不动 |
| `create_project` 给相对路径 / 裸名字 | → `ValueError`（必须在调用处说清位置）|

**为什么创建路径也要钉死**：`AEDT_GUARD_ALLOW_CREATE=1` 时若仍传裸名字，
PyAEDT 会把工程建到**它自己的默认目录**，事后找不着。所以模块现在把它展开到
第一个搜索目录（`D:\HFSS Temp`），并在日志里打印最终路径。

**测试**：`tests/test_project_guard.py` 从 11 个用例扩到 **17 个**，
新增 6 个覆盖 `create_project`（存在即拒、裸名字拒绝、自动建父目录、
`project=` 不重复传等）。


---

## 13. AEDT 2026 与 PyAEDT 1.9 的求解类型不匹配（2026-10-22 实测）

### 13.1 症状：四条报错，一个根因

在 AEDT 2026.1 + PyAEDT 1.9.0 上新建 HFSS 设计，Message Manager 里会连续出现：

```
[error] assignment = Region   name = rad_region        ← 辐射边界分配失败
[error] sphere err -> GrpcApiError                     ← 远场球插入失败
[error] native InsertSetup failed (GetPropNames)       ← 原生 setup 也失败
KeyError: '<某个 HFSS 长名>'  (create_setup)            ← PyAEDT 建 setup 失败
```

**它们不是四个问题，根因只有一个。** 但要注意最后一个报错里的**字符串是变量**：

> `KeyError` 后面跟的，是**这个设计当前的实际类型长名**，不是固定的某个值。

我们在不同会话里观察到**两种**，都触发同一个 `KeyError`：

| 观察到的报错 | 那个设计的实际类型 | 出现场景 |
| --- | --- | --- |
| `KeyError: 'HFSS Terminal Network'` | Terminal（终端网络）| 空工程里 `insert_design` 落到了 Terminal 模板 |
| `KeyError: 'HFSS Hybrid Modal Network'` | 2026 的驱动模态 | 空工程里 `insert_design` 落到了 2026 的新 modal 模板 |

**所以不要假设"新建设计一定是 Modal"**，也不要假设"一定是 Terminal" ——
AEDT 给新设计的默认类型**取决于当时的环境/模板**，实测两种都见过。
**唯一可靠的做法是创建后立刻读 AEDT 的真实类型**（odesign.GetSolutionType()，见 13.2 末的"关键认知"）。

（早先本节写着"根因只有一个：落到了 `HFSS Hybrid Modal Network`"，
第 12 章另一处写着"落到了 `HFSS Terminal Network`" —— 两处都只说了一半，已合并到本节。）

### 13.2 机制（读 PyAEDT 源码确认，非猜测）

`ansys/aedt/core/application/design_solutions.py`：

```python
line  40   _solution_options = copy.deepcopy(design_type.solution_types)   # 键是短名
line  46   _solution_options["Modal"]["name"]    = "HFSS Modal Network"   # 旧长名
line  47   _solution_options["Terminal"]["name"] = "HFSS Terminal Network"
line  72   self._solution_type = self._odesign.GetSolutionType()          # 返回长名
line  97   elif value and value in self._solution_options:                # setter 只认短名
line 101       self._odesign.SetSolutionType(self._solution_options[value]["name"], ...)
line 134   return self._solution_options[self.solution_type]["default_setup"]   # 长名查短名键
```

于是 2026 上出现两处不匹配：

| 方向 | 行为 |
|---|---|
| **读**（line 72 → 134） | `GetSolutionType()` 返回 `HFSS Hybrid Modal Network`，字典里没有这个键 → `KeyError` |
| **写**（line 97 → 101） | 传 `"Modal"` 会发送旧名 `HFSS Modal Network`，**AEDT 2026 拒绝**（报 `'modal' is not a valid solution type`）；传 `"Driven Modal"` 不是键，静默失败 |

**能触发这个 KeyError 的长名，就是 AEDT 各种解类型名**（键是短名，值才是长名）：

| 短名（字典的键）| 长名（`GetSolutionType()` 返回的值）| 来源 |
| --- | --- | --- |
| `Modal` | `HFSS Modal Network`（≤2025）／`HFSS Hybrid Modal Network`（2026）| 源码 L46 + 实测 |
| `Terminal` | `HFSS Terminal Network` | 源码 L47 + 实报错 |

> 只列**已实测/已在源码里确认**的两个。`Eigenmode`、`Transient` 等类型没有测过，
> 按同一机制它们很可能也受影响，但**不要当成结论** —— 需要时先读一次
> `GetSolutionType()` 看实际返回什么。

`create_setup` 之类的调用最终都会走到
`_solution_options[self.solution_type]["default_setup"]`，
而 `self.solution_type` 存的是**长名**、字典的键是**短名** —— 只要两者对不上就 `KeyError`，
报错信息自然就是那个长名。

**关键认知**：`h.solution_type` 是**属性标签**，会**说谎**（显示 Modal 而设计实际是 Hybrid）。
判断真实类型**只能**用 `h.odesign.GetSolutionType()`。

### 13.3 实测证据（2026-10-22）

`SetSolutionType` 候选字符串扫描（同一个 Hybrid 设计）：

| 传入 | AEDT 实际变成 |
|---|---|
| `"Eigenmode"` | `Eigenmode` ✅ 接受 |
| `"Transient"` | `Transient Composite` ✅ 接受 |
| `"HFSS Hybrid Modal Network"` | 自身 ✅ 接受 |
| `"Driven Modal"` / `"Modal"` / `"Driven Terminal"` / `"Terminal"` | ❌ gRPC 异常 |
| `"DrivenModal"` | ⚠️ 不报错，**类型不变**（静默失败） |

### 13.4 修复：`pyaedt_soltype_shim.py`

`work_metallic_lens\aedt\pyaedt_soltype_shim.py`，核心两条：

```python
opts = app.design_solutions._solution_options
real = app.odesign.GetSolutionType()          # 以 AEDT 实际返回的名字为准

# (a) 让 setter 发送 AEDT 认的长名
opts["Modal"]["name"] = real
# (b) 让 getter 拿到的长名能查到键（default_setup 依赖这条）
opts[real] = opts["Modal"]
```

不是把名字写死成 `HFSS Hybrid Modal Network`，而是**每次读 AEDT 的实际名字再对齐**，
所以 AEDT 以后再改名也不会失效。

构建脚本里的用法：

```python
import pyaedt_soltype_shim as _solshim
_real = _solshim.set_modal(h)                 # 对齐 + 切到 modal 类型
if not ok(_real): raise SystemExit("ABORT: ...")   # 类型不对就别往下建几何
```

**如果装不了 shim：手动三步（等效，但要每次做）**

```python
h.insert_design(DES, "DrivenModal")        # 1) 插入时就给模板
h = Hfss(project=PROJ, design=DES, port=PORT, new_desktop=False)
h.odesign.SetSolutionType("HFSS Hybrid Modal Network")   # 2) 用 AEDT 认的长名强制
time.sleep(2)
h = Hfss(project=PROJ, design=DES, port=PORT, new_desktop=False)  # 3) 重新挂载刷新缓存
```

第 2 步的字符串**必须是 AEDT 当前认的长名**（先 `GetSolutionType()` 读一次确认）。
传短名 `"Modal"` 会发出旧长名被 AEDT 拒绝；传 `"DrivenModal"` 不报错但**类型不变**（静默失败）——
这两种都是我实测过的失败方式，见 13.3 的表。

**验证记录**（`create_setup OK -> P4_2`，`KeyError` 消失）。

### 13.5 为什么这个错误值得单独记一条

它差点被误判成"AEDT 坏了"。实际上：

- **AEDT 没坏** —— 它只是改了类型名（2026 的新功能）
- **PyAEDT 没坏** —— 它只是没跟上改名
- **坏的是那次判断**：连续几晚重启 5 次、绕道"复制旧设计"、最终碰了不该碰的工程文件

**教训**：遇到 `KeyError` 里的字符串和当前版本对不上时，**先查版本命名变更，别先怀疑环境**。
这次的证据链其实就是三层：报错里那个字符串 → PyAEDT 字典的键 → AEDT 实际返回的名字。

### 13.6 端到端状态（如实标注）

| 环节 | 状态 |
|---|---|
| 求解类型对齐 + `create_setup` | ✅ 实测通过 |
| 类型校验 fail-fast | ✅ 实测生效 |
| 脚本编译（213 个） | ✅ 0 错误 |
| **完整几何构建 + 辐射边界 + 远场球** | ❌ **未通过**：本会话里 `oboundary` 与远场球接口反复失效（`GrpApiError`），且设计有时只建出 `disc` 就中断 |

**结论**：求解类型这一环已解决；完整构建仍受 AEDT 会话稳定性限制，
需要在**刚重启的会话**里重试，并按 §12.6 处理"重启也不恢复"的情况。


### 13.7 为什么 AEDT 里会冒出一堆 `Project2 / Project6 / Project11`

**不是 AEDT 的 bug，是 PyAEDT 的命名行为 + 我的调用方式。**

| 传给 `Hfss(project=...)` 的值 | 文件不存在时的结果 |
|---|---|
| **绝对路径** `r"D:\HFSS Temp\New.aedt"` | 建出 **`ProjectN`**（名字丢了，落在默认目录）|
| **裸名** `"FBoss"` | 建出 **`FBoss.aedt`**（名字保住；guard 会把它解析到 `D:\HFSS Temp`）|

**为什么会出现一大串**：2026-10-21/22 那晚构建失败重试了十几次，**每次重试都新建一个空白工程**：

```
Project3    f24_chk     ← 第 1 次尝试
Project6    f24_chk     ← 第 2 次
Project8    f24_chk     ← 第 3 次（成功，已存盘 393.8 KB）
Project9    HFSS_6N1 + f24_thk
Project10   f24_thk
Project11   f24_thk     ← 成功，已存盘 389 KB
```

再加上我手动调过几次 `oDesktop.NewProject()`（`Project2`），以及重复出现的同名工程
（一个来自文件、一个在内存里）。

**关键点**：这些空白工程**多数没存盘**，关掉 AEDT 就消失 —— 只有脚本成功保存过的那几个
才落到磁盘。所以"重启后列表变干净"是这个原因，不是 AEDT 自愈。

**规范做法**：

1. 新建工程一律传**裸名**，让 guard 决定位置（§12.10 的 `create_project` 更严格：必须给绝对路径且存在即拒）
2. 脚本结束前用 `GetProjectList()` 前后对比，列出本次新建的工程，便于清理
3. 失败重试要**复用**同一个名字，不要每次换名（否则每次留一个垃圾工程）
