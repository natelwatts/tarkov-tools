"""System-wide hotkey registration via RegisterHotKey.

RegisterHotKey is the polite way to claim a key combination on Windows: the
OS routes the keypress to us. It is not a keyboard hook and it does not
observe any other keystrokes, so it sees nothing except the combination the
user chose.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_MODIFIER_NAMES = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
}

# Virtual key codes for names that are not a single character.
_VK_NAMES = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "insert": 0x2D,
    "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91, "pause": 0x13,
    "tilde": 0xC0, "grave": 0xC0, "backtick": 0xC0,
}
for _i in range(1, 25):
    _VK_NAMES[f"f{_i}"] = 0x6F + _i

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class HotkeyError(RuntimeError):
    pass


def parse_hotkey(spec: str) -> tuple[int, int]:
    """Turn something like 'ctrl+alt+t' into (modifiers, virtual key code)."""
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("empty hotkey specification")

    modifiers = 0
    key: str | None = None
    for part in parts:
        if part in _MODIFIER_NAMES:
            modifiers |= _MODIFIER_NAMES[part]
        else:
            key = part

    if key is None:
        raise HotkeyError(f"hotkey {spec!r} has no non-modifier key")

    if key in _VK_NAMES:
        vk = _VK_NAMES[key]
    elif len(key) == 1:
        vk = ord(key.upper())
    else:
        raise HotkeyError(f"unrecognised key {key!r} in hotkey {spec!r}")

    return modifiers | MOD_NOREPEAT, vk


class HotkeyListener:
    """Runs a Win32 message loop on its own thread and fires a callback.

    The callback runs on the listener thread, so if it touches a GUI it must
    hand off to that GUI's own loop rather than drawing directly.
    """

    def __init__(self, spec: str, callback):
        self.spec = spec
        self.callback = callback
        self.modifiers, self.vk = parse_hotkey(spec)
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, self.modifiers, self.vk):
            err = ctypes.get_last_error()
            hint = " (another application already owns it)" if err == 1409 else ""
            self._error = HotkeyError(f"could not register {self.spec!r}: error {err}{hint}")
            self._ready.set()
            return

        self._ready.set()
        try:
            msg = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):
                    break
                if msg.message == WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception as exc:  # keep the loop alive
                        print(f"hotkey callback error: {exc}")
        finally:
            user32.UnregisterHotKey(None, 1)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="hotkey")
        self._thread.start()
        self._ready.wait(timeout=5)
        if self._error:
            raise self._error

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)
