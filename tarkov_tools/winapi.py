"""Thin ctypes wrappers around the Win32 calls this project needs.

Deliberately narrow: display gamma ramps, foreground-window identification,
and global hotkey registration. No process memory access, no injection, and
no handle opened against the game beyond PROCESS_QUERY_LIMITED_INFORMATION
for reading an executable name.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# GAMMARAMP is three consecutive arrays of 256 WORDs: red, green, blue.
GammaRamp = ctypes.c_ushort * 256 * 3

DISPLAY_DEVICE_ACTIVE = 0x00000001


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


gdi32.CreateDCW.argtypes = [wintypes.LPCWSTR] * 3 + [ctypes.c_void_p]
gdi32.CreateDCW.restype = wintypes.HDC
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.SetDeviceGammaRamp.restype = wintypes.BOOL
gdi32.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.GetDeviceGammaRamp.restype = wintypes.BOOL

user32.EnumDisplayDevicesW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), wintypes.DWORD
]
user32.EnumDisplayDevicesW.restype = wintypes.BOOL
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFOEXW)]
user32.GetMonitorInfoW.restype = wintypes.BOOL


# --- displays ----------------------------------------------------------

def list_displays(active_only: bool = True) -> list[tuple[str, str]]:
    r"""Return [(device_name, description)] for attached displays.

    device_name looks like r'\\.\DISPLAY1' and is what CreateDC needs.
    Note: the generic GetDC(NULL) / CreateDC("DISPLAY") handles do NOT
    reliably support gamma ramps on multi-GPU-output systems, which is why
    everything here works per-display.
    """
    out: list[tuple[str, str]] = []
    index = 0
    while True:
        dev = DISPLAY_DEVICEW()
        dev.cb = ctypes.sizeof(dev)
        if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(dev), 0):
            break
        if not active_only or (dev.StateFlags & DISPLAY_DEVICE_ACTIVE):
            out.append((dev.DeviceName, dev.DeviceString))
        index += 1
    return out


class DisplayDC:
    """Context manager for a per-display device context that supports gamma."""

    def __init__(self, device_name: str):
        self.device_name = device_name
        self.hdc = None

    def __enter__(self) -> wintypes.HDC:
        self.hdc = gdi32.CreateDCW(self.device_name, self.device_name, None, None)
        if not self.hdc:
            raise OSError(f"CreateDC failed for {self.device_name}")
        return self.hdc

    def __exit__(self, *exc):
        if self.hdc:
            gdi32.DeleteDC(self.hdc)
        return False


def get_gamma_ramp(device_name: str) -> GammaRamp:
    ramp = GammaRamp()
    with DisplayDC(device_name) as hdc:
        if not gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(ramp)):
            raise OSError(f"GetDeviceGammaRamp failed for {device_name}")
    return ramp


def set_gamma_ramp(device_name: str, ramp: GammaRamp) -> bool:
    with DisplayDC(device_name) as hdc:
        return bool(gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)))


MONITOR_DEFAULTTONEAREST = 0x00000002


def display_for_window(hwnd) -> str | None:
    """Return the display device name a window currently sits on."""
    if not hwnd:
        return None
    hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return None
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        return None
    return info.szDevice


# --- foreground window / process ---------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


GA_ROOT = 2

user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wintypes.HWND


def toplevel_of(hwnd):
    """The real top-level window for a handle.

    Tk's winfo_id() hands back a TkChild whose title is empty; the titled
    window is its GA_ROOT ancestor. Anything that needs a title, a class, or
    SetForegroundWindow must use this rather than the raw handle.
    """
    if not hwnd:
        return hwnd
    return user32.GetAncestor(hwnd, GA_ROOT) or hwnd


def window_title(hwnd) -> str:
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def window_class(hwnd) -> str:
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    user32.GetClassNameW(hwnd, buf, 512)
    return buf.value


def foreground_window():
    return user32.GetForegroundWindow()


def exe_name_for_window(hwnd) -> str | None:
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)
    return None


def foreground_exe_name() -> str | None:
    """The .exe filename of the focused window's process, or None."""
    return exe_name_for_window(user32.GetForegroundWindow())
