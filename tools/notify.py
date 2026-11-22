# -*- coding: utf-8 -*-
"""Small Windows desktop notification helper for the Builder Approver.

Uses the built-in Windows toast API through PowerShell's WinRT bridge.  No
third-party package is required.  When toast delivery is unavailable the caller
can still fall back to a console/log line and a bell.
"""
from __future__ import annotations

import base64
import subprocess
import sys
from xml.sax.saxutils import escape

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
# Registered by tools/register_windows_notifications.py.  An unpackaged app
# needs a Start-menu shortcut carrying this AUMID or Windows drops the toast.
AUMID = "Hawse1337.AEDTBuilderApprover"


def windows_toast(title: str, message: str, *, sound: bool = True) -> bool:
    """Show a Windows toast; return True if the helper process was started."""
    if sys.platform != "win32":
        return False
    xml = (
        '<toast><visual><binding template="ToastGeneric"><text>%s</text><text>%s</text>'
        "</binding></visual>%s</toast>"
        % (
            escape(str(title)),
            escape(str(message)),
            '<audio src="ms-winsoundevent:Notification.Default"/>' if sound else "",
        )
    )
    encoded_xml = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    script = (
        "$ErrorActionPreference='Stop';"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType=WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType=WindowsRuntime] | Out-Null;"
        "$xmlText=[Text.Encoding]::UTF8.GetString("
        "[Convert]::FromBase64String('%s'));"
        "$xml=New-Object Windows.Data.Xml.Dom.XmlDocument;"
        "$xml.LoadXml($xmlText);"
        "$toast=New-Object Windows.UI.Notifications.ToastNotification $xml;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'%s').Show($toast);"
    ) % (encoded_xml, AUMID)
    encoded_command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                encoded_command,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
        return True
    except Exception:  # noqa: BLE001
        try:
            import winsound

            winsound.MessageBeep()
        except Exception:  # noqa: BLE001
            pass
        return False
