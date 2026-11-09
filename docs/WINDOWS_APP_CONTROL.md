# Windows Smart App Control（SAC）与 PyAEDT 的兼容问题

## 症状

在启用了 **Smart App Control** 的 Windows 11 机器上，PyAEDT 连接 AEDT 时可能出现下列现象（顺序不固定）：

- 首次连接报 `OSError(22, '应用程序控制策略已阻止此文件。')`（英文：`Application control policy has blocked this file`），WinError `4551` / 状态码 `0xC0E90002`；
- 或连接看似成功，但之后每条 AEDT 命令都失败，例如
  `Failed to execute gRPC AEDT command: GetRegistryString / Save / ValidateDesign / GetSetups`；
- 无限球/远场操作报 `InsertInfiniteSphereSetup` 失败，并把同一会话的后续命令一起拖垮。

## 根因

PyAEDT 的 gRPC 插件按下列链加载本地库：

```
python.exe
 └─ PyDesktopPlugin.dll        (已签名, ANSYS Inc.)
     └─ DesktopProxyLib.dll    (未签名)  ← SAC 拦截点
```

`PyDesktopPlugin.dll`（`<AEDT>/PyDesktopPlugin.dll`）与 `ansysedt.exe` 都有 ANSYS Inc. 有效签名，但它们依赖的
`DesktopProxyLib.dll`（`<AEDT>/DesktopProxyLib.dll`）**没有数字签名**。Smart App Control 要求被加载的镜像达到
“Enterprise/可信”签名级别（事件 3077 里 `Requested Signing Level 2 / Validated 1`），因此会拒绝加载它。由于
PyAEDT 在建立连接时就会走这条链，**这与 MCP 补丁版本无关**：v1.3/v1.4/v1.5 都不改变 DLL 的加载方式。

> SAC 是消费级功能：全新安装的 Windows 11 会先进入“评估”阶段，之后可能**自动转为开启**。所以同一台机器
> 可能“昨天正常、今天开始失败”。SAC 一旦关闭，无法在不重装系统的前提下重新开启。

## 本机实测证据（AEDT 2026 R1）

| 文件 | 签名 | SHA256 |
|---|---|---|
| `<AEDT>\ansysedt.exe` | 有效（ANSYS Inc.） | `8e11cdd8…` |
| `<AEDT>\PyDesktopPlugin.dll` | 有效（ANSYS Inc.） | `1879dc1db45b92482b9b878105e9231842403c447c36ca55ed4aacf1a76187d9` |
| `<AEDT>\DesktopProxyLib.dll` | **未签名** | `fd73c85c307dc307acb240bda7a74c7f14e1f8cb0d1f10d579756e2acec8ce5e` |

CodeIntegrity 事件（`Microsoft-Windows-CodeIntegrity/Operational`）：`3077`（拦截原因）、`3033`、`3089`（签名信息）、`3118`（"Smart App Control Block Details"）。

诊断命令：

```powershell
(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy').VerifiedAndReputablePolicyState   # 0=off 1=enforce 2=evaluate
Get-AuthenticodeSignature "<AEDT>\DesktopProxyLib.dll" | Select-Object Status,StatusMessage
Get-WinEvent -LogName 'Microsoft-Windows-CodeIntegrity/Operational' -MaxEvents 20 |
  Where-Object { $_.Id -in 3077,3033,3089,3118 } | Format-List TimeCreated,Id,Message
```

## 可选方案（按对 SAC 的影响排序）

### 1. 关闭 Smart App Control（最简单，需用户明确同意）

Windows 安全中心 → 应用和浏览器控制 → Smart App Control → **关闭**。
代价：不可逆（不重装系统无法重新开启），降低了对未知程序的防护。适合个人开发机。

### 2. 交给 Microsoft 做信誉判定（SAC 可保持开启，推荐用于分发给他人）

把未签名依赖提交给 Microsoft 分析；若被判定为安全，SAC/ISG 之后会放行：

- 文件提交入口：<https://www.microsoft.com/en-us/wdsi/filesubmission>
- 提交文件：`<AEDT>\DesktopProxyLib.dll`
  （SHA256 `fd73c85c307dc307acb240bda7a74c7f14e1f8cb0d1f10d579756e2acec8ce5e`）
- 说明：这是 Ansys Electronics Desktop 的运行时依赖，被已签名的 `PyDesktopPlugin.dll` 加载，Python 通过 PyAEDT 使用。

注意：这是“信誉/误报”通道，结果由 Microsoft 判定，不是本地白名单，也无法保证时限。

### 3. 请 Ansys 修复（长期正解）

`PyDesktopPlugin.dll` 有 ANSYS Inc. 签名，而它依赖的 `DesktopProxyLib.dll` 没有。向 Ansys 支持提交缺陷，请求：

> `DesktopProxyLib.dll`（AEDT 2026 R1，`<AEDT>\DesktopProxyLib.dll`）未做 Authenticode 签名，导致在启用
> Windows Smart App Control 的 Windows 11 上被 Code Integrity 拦截（0xC0E90002），PyAEDT/gRPC 无法工作。
> 该 DLL 由已签名的 `PyDesktopPlugin.dll` 加载。请对该依赖签名或将其合并进已签名模块。

同目录下其它 Ansys 自身 DLL 已签名，唯独此文件缺失，属于打包遗漏。

### 4. 受管环境：用 WDAC 策略放行

企业环境中通常不用 SAC，而是用 WDAC。若确认是 WDAC（而非 SAC）在拦截，可做**补充策略**按文件哈希/发布者放行：

```xml
<FileRules>
  <Allow Id="ID_ALLOW_DESKTOPPROXY" FileName="DesktopProxyLib.dll"
         MinimumFileVersion="0.0.0.0" Hash="FD73C85C307DC307ACB240BDA7A74C7F14E1F8CB0D1F10D579756E2ACEC8CE5E" />
</FileRules>
```

注意：SAC **不支持**任何本地白名单；只有 WDAC 才能这样加例外，且强制模式下策略需要签名。

### 5. 换执行环境

- 在 **WSL/虚拟机**或**未启用 SAC 的机器**上运行 MCP + PyAEDT；
- 或使用不含 SAC 的 Windows Server / 企业镜像。

## 补丁里的处理

- worker 启动时会检测 SAC 状态（注册表 `VerifiedAndReputablePolicyState`），若为开启则向 stderr 打一条明确警告，
  指引到本文档，避免把 SAC 问题误判成 gRPC/模型问题；
- `preflight_design` 在缺少辐射边界时会明确提示 "InsertInfiniteSphereSetup requires an existing radiation
  boundary"，这是另一个独立的常见坑；
- 以上检测都不改变任何既有行为，仅做提示。
