# 接入 AEDT Guard 的 AI：Builder 审批请求说明

> 适用对象：接入本机 `ansys-aedt-mcp` 守卫的 AI（DeepSeek Harness、Codex 或其他 Agent）。  
> 目的：任何需要修改 builder 代码的 AI，都按同一格式提交“候选文件 + 审批请求”，操作员用 `AEDT Builder Approver` 完成审批，不需要 AI 直接改 allowlist。

## 0. 一句话

**AI 只负责产出候选 builder 文件和审批请求，不负责注册自己。操作员审批后，守卫才允许新代码执行。**

## 1. 什么时候需要请求审批

| 情况 | 是否需要审批 |
|---|---|
| 只改调用参数（`theta_deg`、`h_knee_mm`、`symmetry` 等已声明参数） | 不需要 |
| 修改 builder 脚本任何内容（包括一行积分线、边界、注释） | **需要** |
| 新增参数/枚举值 | **需要更新参数表**（通常和脚本一起） |
| 只修正工程副本，不改 builder | 不需要注册 builder；走 `open_project`/`analyze_design` |
| 想直接改 `approved/p0_model.py` 或 `builders.local.json` | **禁止** |

原因：守卫批准的是**整个 builder 脚本的 SHA-256**。脚本任何一个字节变化都会成为新版本，必须由操作员重新审核。

## 2. 必须提交的三件套

### 2.1 候选 builder 文件

- 保存为真实 `.py` 文件；
- 路径必须唯一，不要和另一个对话共用同一个候选文件：

```text
C:\Users\Administrator\Desktop\da project\scripts\candidates\p0_model_codex_20261122_2310.py
```

- 候选文件必须是最终要执行的完整文件，不是 diff 片段；
- 不要直接覆盖 `scripts\p0_model.py`，除非你确认没有其他对话在用它。

### 2.2 审批请求 sidecar

在候选文件旁边生成：

```text
<候选文件名去掉 .py>.approval-request.json
```

例如：

```text
p0_model_codex_20261122_2310.approval-request.json
```

### 2.3 给操作员的简短消息

在聊天里再发一个摘要，不要只丢文件路径。

## 3. sidecar 标准格式

```json
{
  "schema_version": 2,
  "builder_id": "p0_model",
  "request_type": "builder_change",
  "candidate": "C:\\Users\\Administrator\\Desktop\\da project\\scripts\\candidates\\p0_model_codex_20261122_2310.py",
  "candidate_sha256": "<候选文件的真实 SHA-256，64 位小写>",
  "old_approved_sha256": "<当前 allowlist 中的 hash>",
  "platform": "Codex",
  "session_id": "<会话 ID>",
  "session_title": "<会话标题>",
  "requested_at": "2026-11-22T23:10:00+08:00",
  "reason": "修复半模型端口积分线与馈电腔/空气圆柱边界",
  "target_designs": ["Trunc_10GHz"],
  "change_summary": [
    "apex 端口积分线在 symmetry=x 时移到 x=0.5*apex_r_mm",
    "馈电腔增加 Perfect H",
    "空气圆柱侧壁增加 Radiation"
  ],
  "new_params": [
    {
      "name": "symmetry",
      "type": "enum",
      "values": ["", "none", "x"],
      "required": false
    }
  ],
  "new_enum_values": {
    "family": ["APEX"],
    "feed": ["apex"]
  },
  "tested": [
    "python -m py_compile <candidate>",
    "python scripts/py_static_check.py <candidate>"
  ],
  "risk_notes": "只影响 symmetry=x 半模型；全模型路径保持不变",
  "operator_action": "检查候选 diff、补参数表、批准并更新 allowlist"
}
```

### 字段要求

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | 当前用 `2` |
| `builder_id` | 是 | 例如 `p0_model` |
| `request_type` | 是 | `builder_change` / `schema_only` |
| `candidate` | 是 | 绝对路径 |
| `candidate_sha256` | 是 | 真实文件 hash，不要写旧值 |
| `old_approved_sha256` | 是 | 当前 allowlist hash |
| `platform` / `session_id` / `session_title` | 是 | GUI 用来显示来源 |
| `requested_at` | 是 | ISO 8601，带时区 |
| `reason` | 是 | 一句话说明为什么改 |
| `change_summary` | 是 | 代码变更列表 |
| `new_params` | 视情况 | 新参数或参数范围变化 |
| `new_enum_values` | 视情况 | 现有枚举新增值 |
| `tested` | 是 | 语法/静态检查结果 |
| `risk_notes` | 建议 | 影响范围 |

如果只是参数表缺项、脚本没有变化：

```json
{
  "schema_version": 2,
  "builder_id": "p0_model",
  "request_type": "schema_only",
  "candidate": "<当前已批准脚本路径>",
  "candidate_sha256": "<当前已批准 hash>",
  "platform": "Codex",
  "session_id": "...",
  "requested_at": "...",
  "reason": "需要允许调用方传 symmetry",
  "new_params": [...],
  "new_enum_values": {...},
  "operator_action": "只更新参数表，不需要重新批准脚本 hash"
}
```

## 4. 给 AI 的固定工作步骤

1. **确认要改的是 builder 还是工程副本**
   - 改代码 → 走审批；
   - 只改工程/设置 → 走 guard 的工程工具。
2. **从当前 approved 版本复制候选**，不要从旧版本改：
   ```powershell
   Copy-Item C:\ProgramData\ansys-aedt-mcp\approved\p0_model.py `
             "C:\...\scripts\candidates\p0_model_<session>_<timestamp>.py"
   ```
3. **只修改候选文件**。
4. **保存后计算真实 hash**：
   ```powershell
   (Get-FileHash -Algorithm SHA256 -LiteralPath <candidate>).Hash.ToLower()
   ```
5. **做语法/静态检查**，记录命令和结果。
6. **生成 sidecar**，字段齐全。
7. **在聊天里发摘要**，不要只说“已生成”。
8. **不要尝试自己批准、改 allowlist、改 approved 副本**。
9. 等待操作员批准后，重新读取 allowlist 确认：
   - `sha256` == 候选 hash；
   - 参数表包含你需要的参数/枚举。
10. 再调用 `run_approved_builder`，参数里带齐 `design` 和新参数。

## 5. 给操作员的简短消息模板

```text
【Builder 审批请求】
builder: p0_model
候选文件: C:\...\p0_model_codex_20261122_2310.py
候选 SHA-256: 653ee9...
当前已批准 SHA-256: cab97e...
请求类型: builder_change
原因: 修复半模型端口积分线、馈电腔 Perfect H、空气圆柱 Radiation
代码变更:
  - ...
新增参数/枚举:
  - symmetry: ["", "none", "x"]
自检:
  - py_compile: OK
  - 静态检查: OK
sidecar: C:\...\p0_model_codex_20261122_2310.approval-request.json
请打开 AEDT Builder Approver 审核；批准后我会重试 10 GHz 单点求解。
```

## 6. 禁止事项

- ❌ 直接修改 `C:\ProgramData\ansys-aedt-mcp\approved\p0_model.py`
- ❌ 直接修改 `builders.local.json`
- ❌ 用 `run_python_code` 或未受管 AEDT 绕过 builder
- ❌ 用旧 hash 或伪造 `candidate_sha256`
- ❌ 覆盖另一个对话正在使用的候选文件
- ❌ 在未批准前声称“已注册/已生效”

## 7. 为什么“接入的 AI 一般应该知道格式”？

前提是系统把这份契约作为 **skill / system prompt / AGENTS.md** 注入给 AI。  
Codex 这次不知道，不是它笨，而是当前接入只暴露了守卫工具，没有给它一份强制的“builder 审批请求格式”。

更稳的系统设计是：

1. 每个 Agent profile 都加载一份 `builder-approval` skill（本文件可作为技能正文）；
2. `run_approved_builder` 拒绝时返回结构化 `approval_required`，附带 `request_id`、`required_fields` 和模板；
3. 守卫/GUI 校验 sidecar；操作员只处理“差异 + 是否允许”；
4. 这样 AI 不需要记忆格式，格式由系统下发和校验。

## 8. 可直接给 GPT 的短版系统提示

```text
你是接入本机 AEDT Guard 的工程 AI。修改 builder 代码前必须请求操作员审批：
1. 从当前 approved 版本复制出唯一候选文件，路径写到：
   <project>\scripts\candidates\<builder>_<session>_<timestamp>.py
2. 在候选文件旁生成 <candidate_stem>.approval-request.json，至少包含：
   schema_version, builder_id, request_type, candidate, candidate_sha256,
   old_approved_sha256, platform, session_id, session_title, requested_at,
   reason, change_summary, new_params, new_enum_values, tested, risk_notes。
3. 计算真实 SHA-256，并执行 py_compile 与静态检查。
4. 不允许修改 approved/、builders.local.json 或守卫配置；不允许自己批准。
5. 在聊天中提交“Builder 审批请求”摘要；等待操作员批准后，重新读取 allowlist
   确认 sha256/参数表，再调用 run_approved_builder。
```
