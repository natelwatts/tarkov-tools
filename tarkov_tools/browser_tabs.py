"""Reuse an already-open Chrome tab instead of piling up duplicates.

Chrome has no command-line switch for "focus the tab showing this page", and
it exposes no local API unless it was started with --remote-debugging-port.
What it does expose is its accessibility tree: every tab is a TabItem element
whose Name is the tab title. So the tab is located through UI Automation,
selected, and then navigated to the exact URL with Ctrl+L.

Everything degrades gracefully - if UI Automation is unavailable or no
matching tab exists, the caller just opens a new tab as before.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

CHROME_WINDOW_CLASSES = ("Chrome_WidgetWin_1",)

# --- window enumeration -------------------------------------------------

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_text(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def chrome_windows() -> list[int]:
    """Visible, titled Chrome top-level windows."""
    found: list[int] = []

    def callback(hwnd, _lparam):
        if (
            user32.IsWindowVisible(hwnd)
            and _class_name(hwnd) in CHROME_WINDOW_CLASSES
            and _window_text(hwnd)
        ):
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


# --- UI Automation ------------------------------------------------------

def _uia():
    """The UI Automation client, or None if it cannot be created."""
    try:
        from comtypes.client import CreateObject, GetModule

        GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        return CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation), UIA
    except Exception:
        return None


UIA_DOCUMENT_CONTROL_TYPE = 50030


def _is_browser_tab(uia, UIA, element) -> bool:
    """True for a tab-strip TabItem, false for a TabItem inside a web page.

    Pages render their own tab widgets - Discord's channel tabs, a tracker's
    ALL/TASKS/HIDEOUT - and those are also TabItem elements. The difference is
    that page content sits under a Document element while the browser's tab
    strip does not, so the ancestor chain is what distinguishes them.
    """
    try:
        walker = uia.ControlViewWalker
        node = walker.GetParentElement(element)
        for _ in range(12):  # depth cap; the strip is only a few levels up
            if node is None:
                return True
            if node.CurrentControlType == UIA_DOCUMENT_CONTROL_TYPE:
                return False
            node = walker.GetParentElement(node)
    except Exception:
        return True
    return True


def find_tab(title_fragment: str) -> tuple[int, object] | None:
    """Locate a Chrome browser tab whose title contains title_fragment.

    Returns (hwnd, tab_element). The search is scoped to each Chrome window
    rather than the whole desktop, which keeps it fast.
    """
    created = _uia()
    if not created:
        return None
    uia, UIA = created
    needle = title_fragment.lower()

    for hwnd in chrome_windows():
        try:
            root = uia.ElementFromHandle(ctypes.c_void_p(hwnd))
            if not root:
                continue
            condition = uia.CreatePropertyCondition(
                UIA.UIA_ControlTypePropertyId, UIA.UIA_TabItemControlTypeId
            )
            tabs = root.FindAll(UIA.TreeScope_Subtree, condition)
            for index in range(tabs.Length):
                tab = tabs.GetElement(index)
                name = (tab.CurrentName or "").lower()
                if needle in name and _is_browser_tab(uia, UIA, tab):
                    return hwnd, tab
        except Exception:
            continue
    return None


def list_browser_tabs() -> list[tuple[int, str]]:
    """[(hwnd, title)] for real browser tabs only - useful for diagnosis."""
    created = _uia()
    if not created:
        return []
    uia, UIA = created
    out: list[tuple[int, str]] = []
    for hwnd in chrome_windows():
        try:
            root = uia.ElementFromHandle(ctypes.c_void_p(hwnd))
            condition = uia.CreatePropertyCondition(
                UIA.UIA_ControlTypePropertyId, UIA.UIA_TabItemControlTypeId
            )
            tabs = root.FindAll(UIA.TreeScope_Subtree, condition)
            for index in range(tabs.Length):
                tab = tabs.GetElement(index)
                if _is_browser_tab(uia, UIA, tab):
                    out.append((hwnd, tab.CurrentName or ""))
        except Exception:
            continue
    return out


def select_tab(tab) -> bool:
    """Bring a tab to the front of its window."""
    try:
        from comtypes.gen import UIAutomationClient as UIA

        pattern = tab.GetCurrentPattern(UIA.UIA_SelectionItemPatternId)
        if pattern:
            pattern.QueryInterface(UIA.IUIAutomationSelectionItemPattern).Select()
            return True
    except Exception:
        pass
    try:
        from comtypes.gen import UIAutomationClient as UIA

        pattern = tab.GetCurrentPattern(UIA.UIA_InvokePatternId)
        if pattern:
            pattern.QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()
            return True
    except Exception:
        pass
    return False


# --- keyboard input -----------------------------------------------------

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL = 0x11
VK_RETURN = 0x0D
VK_L = 0x4C


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


def _send(inputs: list[INPUT]) -> None:
    array = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))


def _key(vk: int, up: bool = False) -> INPUT:
    return INPUT(INPUT_KEYBOARD, _INPUTunion(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)))


def _char(ch: str) -> list[INPUT]:
    code = ord(ch)
    down = INPUT(INPUT_KEYBOARD, _INPUTunion(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, None)))
    up = INPUT(INPUT_KEYBOARD, _INPUTunion(
        ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)))
    return [down, up]


def type_url_into_address_bar(url: str) -> None:
    """Ctrl+L, type the URL, Enter - navigating the current tab in place.

    Typed as Unicode scan codes so it does not depend on the keyboard layout,
    and so the clipboard is left untouched.
    """
    _send([_key(VK_CONTROL), _key(VK_L), _key(VK_L, up=True), _key(VK_CONTROL, up=True)])
    time.sleep(0.12)
    batch: list[INPUT] = []
    for ch in url:
        batch.extend(_char(ch))
    if batch:
        _send(batch)
    time.sleep(0.08)
    _send([_key(VK_RETURN), _key(VK_RETURN, up=True)])


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _raise_window(hwnd) -> None:
    """Bring a window forward, attaching to the foreground thread if needed."""
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE, in case it is minimised
        if user32.SetForegroundWindow(hwnd):
            return
        current = user32.GetForegroundWindow()
        if not current:
            return
        target_tid = user32.GetWindowThreadProcessId(current, None)
        our_tid = kernel32.GetCurrentThreadId()
        attached = bool(user32.AttachThreadInput(target_tid, our_tid, True))
        try:
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(target_tid, our_tid, False)
    except Exception:
        pass


def focus_existing_tab(title_fragment: str, url: str) -> bool:
    """Focus a matching Chrome tab and navigate it to url in place."""
    match = find_tab(title_fragment)
    if not match:
        return False
    hwnd, tab = match
    if not select_tab(tab):
        return False
    _raise_window(hwnd)
    time.sleep(0.3)
    if user32.GetForegroundWindow() != hwnd:
        # Never type a URL into whatever else happens to be focused.
        return False
    type_url_into_address_bar(url)
    return True
