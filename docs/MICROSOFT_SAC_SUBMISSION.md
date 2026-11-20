# Microsoft 提交说明：DesktopProxyLib.dll（Smart App Control 拦截）

> 用途：向 Microsoft 提交被 Smart App Control（SAC）拦截的 Ansys 运行时依赖，争取信誉放行，从而**在不关闭 SAC 的前提下**继续使用 PyAEDT/AEDT 自动化。
> 提交入口：<https://www.microsoft.com/en-us/wdsi/filesubmission>
> 本文件只准备文案；是否提交、用哪个账号由使用者决定。

---

## 1. 提交前的准备

| 项目 | 值 |
|---|---|
| 文件 | `<AEDT 安装目录>\DesktopProxyLib.dll`（本例 `D:\ANSYS Inc\v261\AnsysEM\DesktopProxyLib.dll`） |
| 文件大小 | 755 712 字节 |
| SHA256 | `fd73c85c307dc307acb240bda7a74c7f14e1f8cb0d1f10d579756e2acec8ce5e` |
| 加载者 | `<AEDT 安装目录>\PyDesktopPlugin.dll`（签名有效，签名者 **ANSYS Inc.**，SHA256 `1879dc1db45b92482b9b878105e9231842403c447c36ca55ed4aacf1a76187d9`） |
| 宿主进程 | `python.exe`（PyAEDT 通过 `ctypes.PyDLL` 加载 PyDesktopPlugin.dll） |
| 现象 | Code Integrity 拦截，状态 `0xC0E90002`；Python 侧 `OSError(22, '应用程序控制策略已阻止此文件。')` |

先在提交页顶部用 **SHA256** 搜索该哈希（`Search file hash`），若已存在记录可直接查看/补充；不存在再走上传。

---

## 2. 逐字段填写建议

| 表单字段 | 建议内容 |
|---|---|
| **Submit file as a** | `Home customer`（个人/小规模使用）或 `Enterprise customer`（企业账号）。**不要**选 `Software developer`——那是软件厂商验证自家产品检测用的。 |
| **File** | 上传 `DesktopProxyLib.dll`（或直接填写 SHA256 命中已有记录） |
| **Email addresses**（必填） | 你的可收信邮箱（提交后会在 `submission history` 里跟踪结果） |
| **Additional users** | 可留空 |
| **Select the Microsoft security product used to scan the file**（必填） | **Smart App Control** |
| **Detection name / 检测名称**（若出现） | 无（Defender 未报毒；这是 Code Integrity/SAC 的策略拦截，不是病毒检测）。若表单强制要求，填 `N/A – blocked by Smart App Control code integrity (0xC0E90002)` |
| **Product / OS 版本** | Windows 11（本机 `10.0.26200`），Defender 平台与安全情报版本可在 `Get-MpComputerStatus` 里查 |
| **Is this a false positive?**（若出现） | Yes / 我认为该文件是正常文件 |
| **What is wrong with the detection?**（若出现） | 选择“文件被误判/被错误阻止”，并在描述里说明这是未签名的第三方运行时依赖，不是恶意软件 |

---

## 3. 描述正文（English，可直接粘贴）

> **Summary**
> `DesktopProxyLib.dll` (SHA256 fd73c85c307dc307acb240bda7a74c7f14e1f8cb0d1f10d579756e2acec8ce5e, 755,712 bytes) is an
> unsigned runtime dependency shipped with Ansys Electronics Desktop 2026 R1 (`D:\ANSYS Inc\v261\AnsysEM\`).
> Smart App Control on Windows 11 blocks it with Code Integrity status `0xC0E90002`
> ("application control policy has blocked this file"), which breaks legitimate engineering automation.
>
> **Chain of loading**
> 1. A Python process running Ansys PyAEDT loads `PyDesktopPlugin.dll` from the same install directory.
>    That file **is validly signed by ANSYS Inc.** (SHA256 1879dc1db45b92482b9b878105e9231842403c447c36ca55ed4aacf1a76187d9).
> 2. `PyDesktopPlugin.dll` depends on `DesktopProxyLib.dll`, which has **no Authenticode signature**
>    (`Get-AuthenticodeSignature` returns NotSigned). Smart App Control requires a higher signing level for loaded
>    images, so it refuses this dependency.
> 3. The Python caller then receives `OSError(22, 'Application control policy has blocked this file')`; every
>    subsequent AEDT session operation fails with unrelated gRPC errors, so users cannot tell what happened.
>
> **Evidence (CodeIntegrity operational log)**
> - Event 3077: process `C:\Users\...\Python314\python.exe` attempted to load
>   `\Device\HarddiskVolume5\ANSYS Inc\v261\AnsysEM\DesktopProxyLib.dll`, `Requested Signing Level 2`,
>   `Validated Signing Level 1`, `Status 0xc0e90002`.
> - Event 3118: "Smart App Control Block Details" for the same file; policy
>   `{0283ac0f-fff1-49ae-ada1-8a933130cad6}`.
>
> **Why this is a false positive / not malicious**
> - It is a vendor-provided library from an installed commercial CAD/EM product; it is loaded only by that product's
>   own signed plugin, not by a downloaded or obfuscated binary.
> - The installer is Ansys's official AEDT 2026 R1; the file has been present since installation and is not modified.
> - The same directory contains many other Ansys DLLs; this specific dependency simply lacks a signature (likely a
>   packaging oversight on the vendor side). An Ansys support case has been/will be opened to request signing.
>
> **Requested outcome**
> Classify the file as a trusted/normal file (or otherwise allow it under Smart App Control / Defender reputation) so
> that legitimate Ansys AEDT + PyAEDT workflows run with Smart App Control enabled. No behavior change to the file is
> requested.
>
> **Reproduction**
> 1. Windows 11 with Smart App Control enabled.
> 2. Install Ansys Electronics Desktop 2026 R1, then `pip install pyaedt` in a Python 3.14 environment.
> 3. Start AEDT with `ansysedt.exe -grpcsrv <port>`.
> 4. In Python: `from ansys.aedt.core import Desktop; Desktop(port=<port>, new_desktop=False)`.
> 5. Observe `OSError(22, 'Application control policy has blocked this file')` and CI events 3077/3118.

---

## 4. 需要附上的本地诊断输出（可选但加分）

```powershell
(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy').VerifiedAndReputablePolicyState
Get-AuthenticodeSignature "D:\ANSYS Inc\v261\AnsysEM\DesktopProxyLib.dll" | Select-Object Status,StatusMessage
Get-AuthenticodeSignature "D:\ANSYS Inc\v261\AnsysEM\PyDesktopPlugin.dll" | Select-Object Status,@{n='Signer';e={$_.SignerCertificate.Subject}}
Get-FileHash "D:\ANSYS Inc\v261\AnsysEM\DesktopProxyLib.dll" -Algorithm SHA256
Get-WinEvent -LogName 'Microsoft-Windows-CodeIntegrity/Operational' -MaxEvents 20 |
  Where-Object { $_.Id -in 3077,3033,3089,3118 } | Format-List TimeCreated,Id,Message
```

---

## 5. 提交后

- 在 <https://www.microsoft.com/en-us/wdsi/submissionhistory> 用同一账号查看结论；
- 结论可能是：判为安全（放开）/ 维持拦截 / 需要更多信息；
- 若维持拦截，回退方案见 `docs/WINDOWS_APP_CONTROL.md` 的第 1、3、4、5 条（关闭 SAC / 请 Ansys 签名 / WDAC 例外 / 换环境）。
- 同时建议给 Ansys 报缺陷（文案见 `WINDOWS_APP_CONTROL.md` 第 3 节），那是对所有用户一劳永逸的修复。
