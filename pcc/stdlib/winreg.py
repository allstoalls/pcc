"""pcc.stdlib.winreg - non-Windows native compile stub."""

from __future__ import annotations

HKEY_CURRENT_USER = 0
HKEY_LOCAL_MACHINE = 1
KEY_ALL_ACCESS = 0xF003F
REG_BINARY = 3
REG_SZ = 1


class WindowsError(OSError):
    pass


def OpenKey(*args, **kwargs):
    raise WindowsError("winreg is not available on this platform")


def CreateKey(*args, **kwargs):
    raise WindowsError("winreg is not available on this platform")


def SetValueEx(*args, **kwargs):
    raise WindowsError("winreg is not available on this platform")


def QueryValueEx(*args, **kwargs):
    raise WindowsError("winreg is not available on this platform")


def CloseKey(*args, **kwargs):
    return None
