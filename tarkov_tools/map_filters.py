"""Drive the wiki map once it loads: expand it, filter it, frame it.

The map opens zoomed hard into the marker from the URL, which says nothing
about where the extract actually is. So after it loads it is expanded to fill
the page, cut down to the categories worth seeing, zoomed back out to the
whole map, and the marker is re-selected to bring its popup back.

The interactive map takes no filter parameters in its URL - its bundle reads
only `marker`, `canvasEngine`, `skin` and `wgUserId` - and it does not persist
category state. So the sidebar is driven directly.

Categories are plain Text elements with no pattern of their own, but the group
wrapping each one supports UI Automation's Invoke, so they are activated
through the accessibility tree rather than by clicking a screen position.
That matters: coordinate clicks depended on the page having finished scrolling
to the focused marker, on the browser being the foreground window, and on
nothing overlapping the sidebar. Invoke needs none of those.

The sidebar is laid out as section headers with their categories indented
underneath:

    Extractions          <- header
      PMC      Transit   <- categories, two columns
      Scav
    Spawns               <- header
      PMC      Boss
      Scav     Cultists

"PMC" appears under both sections, so a category is identified by name within
its section's vertical band.

This drives someone else's web page, so treat it as best effort: it is wrapped
so any failure leaves the map open and simply unfiltered, and it can be turned
off with `extracts.apply_map_filters`, `extracts.fullscreen_map` and
`extracts.zoom_out_map` in config.json.
"""

from __future__ import annotations

import ctypes
import time

# Header rows sit further left than the categories indented under them.
SECTION_HEADERS = ("Extractions", "Spawns", "Miscellaneous", "Loot")

DEFAULT_WANTED = (("Extractions", "PMC"), ("Extractions", "Scav"), ("Spawns", "PMC"))

# How far up from a label to look for something that can be invoked.
INVOKE_SEARCH_DEPTH = 4


def _labels(uia, UIA, root) -> list[tuple[int, int, str, object, object]]:
    """(top, left, name, rect, element) for every named Text element."""
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
            out.append((rect.top, rect.left, name, rect, element))
    out.sort(key=lambda row: (row[0], row[1]))
    return out


def _find(labels, name: str, top: int | None = None, bottom: int | None = None):
    """The first label with this exact name, optionally inside a vertical band."""
    for row_top, _left, label, _rect, element in labels:
        if label != name:
            continue
        if top is not None and row_top < top:
            continue
        if bottom is not None and row_top >= bottom:
            continue
        return element
    return None


def _section_bands(labels) -> dict[str, tuple[int, int]]:
    """Vertical extent of each sidebar section, so "PMC" can be disambiguated."""
    headers = sorted((row[0], row[2]) for row in labels if row[2] in SECTION_HEADERS)
    bands: dict[str, tuple[int, int]] = {}
    for index, (top, name) in enumerate(headers):
        end = headers[index + 1][0] if index + 1 < len(headers) else top + 10_000
        bands.setdefault(name, (top, end))
    return bands


def _invoke(uia, UIA, element) -> bool:
    """Activate a label by invoking the nearest ancestor that supports it."""
    walker = uia.ControlViewWalker
    node = element
    for _ in range(INVOKE_SEARCH_DEPTH):
        if node is None:
            return False
        try:
            pattern = node.GetCurrentPattern(UIA.UIA_InvokePatternId)
            if pattern:
                pattern.QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()
                return True
        except Exception:
            pass
        try:
            node = walker.GetParentElement(node)
        except Exception:
            return False
    return False


def _read_sidebar(uia, UIA, hwnd):
    """Current sidebar labels, or None if it is not rendered yet."""
    try:
        root = uia.ElementFromHandle(ctypes.c_void_p(hwnd))
        labels = _labels(uia, UIA, root)
    except Exception:
        return None
    if not _find(labels, "Hide All") or not _section_bands(labels):
        return None
    return labels


def _find_named(uia, UIA, hwnd, name: str):
    """Any element with this exact name, whatever its control type.

    The map's own controls are a mix: the zoom buttons really are Buttons,
    but "Enter fullscreen" is a Hyperlink, so searching by type would miss it.
    """
    try:
        root = uia.ElementFromHandle(ctypes.c_void_p(hwnd))
        everything = root.FindAll(UIA.TreeScope_Subtree, uia.CreateTrueCondition())
    except Exception:
        return None
    for index in range(everything.Length):
        element = everything.GetElement(index)
        try:
            if (element.CurrentName or "").strip() == name:
                return element
        except Exception:
            continue
    return None


def enter_fullscreen(hwnd, timeout: float = 20.0, grace: float = 4.0,
                     verbose: bool = False) -> bool:
    """Expand the map to fill the page.

    This is the first thing done to a freshly opened map, so it has to be
    careful about *which* page it is looking at. The window title is already
    "Map:<name>" while the reload is still in flight, so for a moment the
    accessibility tree still describes the previous page - and if that page
    was left expanded, a naive "is it already fullscreen?" check reads its
    stale "Exit fullscreen" control and concludes there is nothing to do.
    The new page then renders unexpanded and stays that way.

    So the wait is for "Enter fullscreen" to appear, which only a rendered,
    unexpanded map offers. "Exit fullscreen" is accepted as already-done only
    after the grace period, by which point the reload has landed.
    """
    from .browser_tabs import _uia

    start = time.monotonic()
    deadline = start + timeout
    uia = UIA = None
    while time.monotonic() < deadline:
        created = _uia()
        if created:
            uia, UIA = created
            control = _find_named(uia, UIA, hwnd, "Enter fullscreen")
            if control and _invoke(uia, UIA, control):
                time.sleep(1.2)
                if verbose:
                    print("  map: fullscreen")
                return True
            if (
                time.monotonic() - start > grace
                and _find_named(uia, UIA, hwnd, "Exit fullscreen")
            ):
                return True  # genuinely expanded already
        time.sleep(0.5)

    if verbose:
        print("  map: could not enter fullscreen")
    return False


def zoom_out(hwnd, presses: int = 10, verbose: bool = False) -> int:
    """Pull back to the whole map.

    Opening on a marker zooms right in, which loses all sense of where the
    extract actually is relative to everything else.
    """
    from .browser_tabs import _uia

    created = _uia()
    if not created:
        return 0
    uia, UIA = created
    done = 0
    for _ in range(presses):
        control = _find_named(uia, UIA, hwnd, "Zoom out")
        if not control:
            break
        try:
            if not control.CurrentIsEnabled:
                break  # already as far out as the map goes
        except Exception:
            pass
        if not _invoke(uia, UIA, control):
            break
        done += 1
        time.sleep(0.35)
    if verbose and done:
        print(f"  map: zoomed out ({done})")
    return done


def show_marker(hwnd, name: str, verbose: bool = False) -> bool:
    """Select a marker by name so its popup opens, without moving the map.

    The ?marker= parameter opens the popup on load, but entering fullscreen
    re-mounts the map and closes it again. The sidebar's search list keeps an
    entry per marker even while collapsed, and invoking one selects that
    marker and opens its popup where it sits - unlike the URL parameter, it
    does not zoom in. So the popup is restored last, after the view is
    already framed the way it should stay.
    """
    from .browser_tabs import _uia

    created = _uia()
    if not created:
        return False
    uia, UIA = created
    entry = _find_named(uia, UIA, hwnd, name)
    if not entry or not _invoke(uia, UIA, entry):
        if verbose:
            print(f"  map: could not open the popup for {name}")
        return False
    time.sleep(0.6)
    if verbose:
        print(f"  map: showing {name}")
    return True


def apply_filters(hwnd, wanted=DEFAULT_WANTED, timeout: float = 25.0,
                  verbose: bool = False) -> bool:
    """Hide every category, then re-enable the wanted ones.

    Returns True if at least one category was activated.
    """
    from .browser_tabs import _uia

    created = _uia()
    if not created:
        return False
    uia, UIA = created

    deadline = time.monotonic() + timeout
    labels = None
    while time.monotonic() < deadline:
        labels = _read_sidebar(uia, UIA, hwnd)
        if labels:
            break
        time.sleep(0.5)
    if not labels:
        if verbose:
            print("  map filters: sidebar not found (page still loading?)")
        return False

    hide_all = _find(labels, "Hide All")
    if not hide_all or not _invoke(uia, UIA, hide_all):
        if verbose:
            print("  map filters: could not activate Hide All")
        return False
    time.sleep(0.4)

    enabled = 0
    for section, category in wanted:
        # Re-read each time: hiding categories can reflow the list, so an
        # element captured earlier may no longer be the row it was.
        labels = _read_sidebar(uia, UIA, hwnd) or labels
        band = _section_bands(labels).get(section)
        if not band:
            continue
        element = _find(labels, category, band[0] + 1, band[1])
        if element and _invoke(uia, UIA, element):
            enabled += 1
            if verbose:
                print(f"  map filters: enabled {section}/{category}")
        time.sleep(0.25)

    if verbose and not enabled:
        print("  map filters: no matching categories")
    return enabled > 0
