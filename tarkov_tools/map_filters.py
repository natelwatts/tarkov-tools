"""Set the wiki map's category filters after it loads.

The interactive map takes no filter parameters in its URL - its bundle reads
only `marker`, `canvasEngine`, `skin` and `wgUserId` - and it does not persist
category state. So the sidebar is driven the only way left: find the controls
in the accessibility tree and click them.

The sidebar is laid out as section headers with their categories indented
underneath:

    Extractions          <- header
      PMC      Transit   <- categories, two columns
      Scav
    Spawns               <- header
      PMC      Boss
      Scav     Cultists

Categories are plain Text elements with no toggle pattern, so they are located
by name within their section's vertical band and clicked by coordinate.

This drives someone else's web page, so treat it as best effort: it is wrapped
so any failure leaves the map open and simply unfiltered, and it can be turned
off with `extracts.apply_map_filters` in config.json.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from .winapi import virtual_screen_bounds

user32 = ctypes.WinDLL("user32", use_last_error=True)

# Header rows sit further left than the categories indented under them.
SECTION_HEADERS = ("Extractions", "Spawns", "Miscellaneous", "Loot")

DEFAULT_WANTED = (("Extractions", "PMC"), ("Extractions", "Scav"), ("Spawns", "PMC"))


# --- mouse --------------------------------------------------------------

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
INPUT_MOUSE = 0


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _MInputUnion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("padding", ctypes.c_ubyte * 32)]


class MINPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _MInputUnion)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(MINPUT), ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]


def _click(x: int, y: int) -> None:
    """Click an absolute virtual-desktop point.

    Absolute mouse coordinates are normalised to 0..65535. Without
    MOUSEEVENTF_VIRTUALDESK that range covers only the primary monitor, which
    would send every click to the wrong screen on a multi-monitor setup where
    a display sits at negative coordinates.
    """
    vx, vy, vw, vh = virtual_screen_bounds()
    nx = int(round((x - vx) * 65535 / max(vw - 1, 1)))
    ny = int(round((y - vy) * 65535 / max(vh - 1, 1)))
    flags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    events = [
        MINPUT(INPUT_MOUSE, _MInputUnion(mi=MOUSEINPUT(nx, ny, 0, flags | MOUSEEVENTF_MOVE, 0, None))),
        MINPUT(INPUT_MOUSE, _MInputUnion(mi=MOUSEINPUT(nx, ny, 0, flags | MOUSEEVENTF_LEFTDOWN, 0, None))),
        MINPUT(INPUT_MOUSE, _MInputUnion(mi=MOUSEINPUT(nx, ny, 0, flags | MOUSEEVENTF_LEFTUP, 0, None))),
    ]
    array = (MINPUT * len(events))(*events)
    user32.SendInput(len(events), array, ctypes.sizeof(MINPUT))


# --- sidebar reading ----------------------------------------------------

def _labels(uia, UIA, root) -> list[tuple[int, int, str, object]]:
    """Every named Text element with a real rectangle, as (y, x, name, rect)."""
    out = []
    found = root.FindAll(
        UIA.TreeScope_Subtree,
        uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_TextControlTypeId),
    )
    for index in range(found.Length):
        element = found.GetElement(index)
        try:
            name = (element.CurrentName or "").strip()
            rect = element.CurrentBoundingRectangle
        except Exception:
            continue
        if name and rect.right > rect.left and rect.bottom > rect.top:
            out.append((rect.top, rect.left, name, rect))
    out.sort()
    return out


def _find(labels, name: str, top: int | None = None, bottom: int | None = None):
    """First label with this exact name, optionally inside a vertical band."""
    for y, _x, label, rect in labels:
        if label != name:
            continue
        if top is not None and y < top:
            continue
        if bottom is not None and y >= bottom:
            continue
        return rect
    return None


def _section_bands(labels) -> dict[str, tuple[int, int]]:
    """Vertical extent of each sidebar section, so 'PMC' can be disambiguated.

    'PMC' appears under both Extractions and Spawns; the band it falls in is
    what tells them apart.
    """
    headers = [(y, name) for y, _x, name, _r in labels if name in SECTION_HEADERS]
    headers.sort()
    bands: dict[str, tuple[int, int]] = {}
    for index, (y, name) in enumerate(headers):
        end = headers[index + 1][0] if index + 1 < len(headers) else y + 10_000
        bands.setdefault(name, (y, end))
    return bands


def apply_filters(hwnd, wanted=DEFAULT_WANTED, timeout: float = 20.0,
                  verbose: bool = False) -> bool:
    """Hide every category, then re-enable the wanted ones.

    Returns True if the sidebar was found and clicked, False otherwise.
    """
    from .browser_tabs import _uia

    created = _uia()
    if not created:
        return False
    uia, UIA = created

    deadline = time.monotonic() + timeout
    labels = []
    hide_all = None
    while time.monotonic() < deadline:
        try:
            root = uia.ElementFromHandle(ctypes.c_void_p(hwnd))
            labels = _labels(uia, UIA, root)
            hide_all = _find(labels, "Hide All")
            if hide_all and _section_bands(labels):
                break
        except Exception:
            pass
        time.sleep(0.7)

    if not hide_all:
        if verbose:
            print("  map filters: sidebar not found (page still loading?)")
        return False

    bands = _section_bands(labels)
    targets = []
    for section, category in wanted:
        band = bands.get(section)
        if not band:
            continue
        rect = _find(labels, category, band[0] + 1, band[1])
        if rect:
            targets.append((f"{section}/{category}", rect))

    if not targets:
        if verbose:
            print("  map filters: no matching categories")
        return False

    # Put the pointer back where it was; nobody expects a lookup to move it.
    origin = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(origin))
    try:
        _click((hide_all.left + hide_all.right) // 2,
               (hide_all.top + hide_all.bottom) // 2)
        time.sleep(0.35)
        for label, rect in targets:
            _click((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
            time.sleep(0.22)
            if verbose:
                print(f"  map filters: enabled {label}")
    finally:
        user32.SetCursorPos(origin.x, origin.y)
    return True
