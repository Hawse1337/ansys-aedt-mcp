# -*- coding: utf-8 -*-
"""Register the approver as a Windows toast-capable desktop app.

Windows drops notifications from unpackaged apps unless the AppUserModelID has
a Start-menu shortcut carrying the same ``System.AppUserModel.ID``.  This
one-time per-user registration is what makes the v10 notifications persist in
the Windows notification center.

Run with the guard venv (pywin32 is available there)::

    python tools/register_windows_notifications.py --exe "C:\\...\\AEDT Builder Approver v10.exe"

It writes only HKCU and the current user's Start-menu folder; no admin rights.
"""
from __future__ import annotations

import argparse
import gc
import os
from pathlib import Path

from notify import AUMID

DISPLAY_NAME = "AEDT Builder Approver"
ROOT = Path(r"C:\ProgramData\ansys-aedt-mcp")
DEFAULT_EXE = ROOT / "tools" / "AEDT Builder Approver.exe"
DEFAULT_ICON = ROOT / "tools" / "assets" / "aedt_builder_approver.ico"


def start_menu_shortcut() -> Path:
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / (DISPLAY_NAME + ".lnk")


def register_registry(icon: Path) -> None:
    import winreg

    key_path = r"Software\Classes\AppUserModelId" + "\\" + AUMID
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE)
    try:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, DISPLAY_NAME)
        winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
        winreg.SetValueEx(key, "IconBackgroundColor", 0, winreg.REG_SZ, "0B1220")
    finally:
        winreg.CloseKey(key)


def enable_notifications(aumid: str) -> dict[str, bool]:
    """Enable Windows toast delivery for this app (and global toasts if off).

    ToastEnabled=0 is a global user setting: leaving it off makes Windows reject
    even correctly registered toasts with PolicyReason=GlobalSettingDisabled.
    The operator explicitly requested desktop approvals, so registration turns
    it on; pass --no-enable-notifications to leave Windows settings untouched.
    """
    import winreg

    changed: dict[str, bool] = {}
    push = winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\PushNotifications",
        0,
        winreg.KEY_READ | winreg.KEY_WRITE,
    )
    try:
        try:
            current, _kind = winreg.QueryValueEx(push, "ToastEnabled")
        except FileNotFoundError:
            current = None
        if current != 1:
            winreg.SetValueEx(push, "ToastEnabled", 0, winreg.REG_DWORD, 1)
            changed["global_toast_enabled"] = True
    finally:
        winreg.CloseKey(push)

    app = winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\\" + aumid,
        0,
        winreg.KEY_WRITE,
    )
    try:
        winreg.SetValueEx(app, "Enabled", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(app, "ShowInActionCenter", 0, winreg.REG_DWORD, 1)
        changed["app_enabled"] = True
    finally:
        winreg.CloseKey(app)
    return changed


def restart_notification_service() -> bool:
    """Best-effort refresh so a just-enabled ToastEnabled takes effect."""
    import subprocess

    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                "Get-Service -Name 'WpnUserService_*' -ErrorAction Stop | Restart-Service -Force -ErrorAction Stop",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def set_shortcut_aumid(shortcut: Path) -> None:
    import pythoncom
    from win32com.propsys import propsys, pscon

    pythoncom.CoInitialize()
    store = propsys.SHGetPropertyStoreFromParsingName(
        str(shortcut), None, 0x2, propsys.IID_IPropertyStore
    )
    try:
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(AUMID))
        store.Commit()
    finally:
        del store
        gc.collect()


def register(exe: Path, icon: Path, *, enable_toasts: bool = True) -> dict[str, str]:
    if os.name != "nt":
        raise SystemExit("Windows only")
    exe = exe.expanduser().resolve()
    icon = icon.expanduser().resolve()
    if not exe.is_file():
        raise SystemExit("exe not found: %s" % exe)
    if not icon.is_file():
        raise SystemExit("icon not found: %s" % icon)

    import win32com.client

    register_registry(icon)
    shortcut = start_menu_shortcut()
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    shell = win32com.client.Dispatch("WScript.Shell")
    link = shell.CreateShortCut(str(shortcut))
    link.Targetpath = str(exe)
    link.WorkingDirectory = str(exe.parent)
    link.IconLocation = "%s,0" % icon
    link.Description = DISPLAY_NAME
    link.Save()
    set_shortcut_aumid(shortcut)
    notification_changes: dict[str, bool] = {}
    service_restarted = False
    if enable_toasts:
        notification_changes = enable_notifications(AUMID)
        service_restarted = restart_notification_service()

    # Read it back to fail closed if the property silently did not stick.
    import pythoncom
    from win32com.propsys import propsys, pscon

    pythoncom.CoInitialize()
    store = propsys.SHGetPropertyStoreFromParsingName(
        str(shortcut), None, 0, propsys.IID_IPropertyStore
    )
    try:
        value = store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
    finally:
        del store
        gc.collect()
    if value != AUMID:
        raise SystemExit("AUMID did not stick on shortcut: %r" % (value,))
    result = {"aumid": AUMID, "exe": str(exe), "shortcut": str(shortcut), "icon": str(icon)}
    if enable_toasts:
        result["global_toast_enabled"] = str(notification_changes.get("global_toast_enabled", False))
        result["notification_service_restarted"] = str(service_restarted)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", default=str(DEFAULT_EXE))
    parser.add_argument("--icon", default=str(DEFAULT_ICON))
    parser.add_argument(
        "--no-enable-notifications",
        action="store_true",
        help="only create the AUMID/shortcut; do not touch Windows notification settings",
    )
    args = parser.parse_args()
    result = register(
        Path(args.exe),
        Path(args.icon),
        enable_toasts=not args.no_enable_notifications,
    )
    for key, value in result.items():
        print("%s: %s" % (key, value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
