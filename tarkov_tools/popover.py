"""Hotkey-summoned search popover.

An ordinary always-on-top window. It does not draw into the game, hook its
input, or read its memory - it is a separate application that happens to sit
above a borderless-windowed game, exactly like Notepad would.

Requires Tarkov to run in borderless windowed mode; exclusive fullscreen
will not composite another window on top.
"""

from __future__ import annotations

import ctypes
import queue
import sqlite3
import threading
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont

from . import db as dbmod
from . import search as searchmod
from .config import load_config
from .config import set_local_override
from .winapi import toplevel_of, virtual_screen_bounds

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WINDOW_TITLE = "Tarkov Tools"

# Markers shown beside anything on one of your lists.
MARK_HAVE = "★"    # filled star - in your stash
MARK_WATCH = "◆"   # diamond - looking out for it

BG = "#14161a"
BG_ALT = "#1b1e24"
FG = "#d7dae0"
FG_DIM = "#7c8391"
ACCENT = "#c8a45c"
SEL = "#2a3038"
TITLE_BG = "#0f1114"

# Column labels for magazine tables, so each row need not repeat them.
MAG_HEADER = "     cap   ergo  name"

# Floor for the popover width; it grows from here to fit the filter bar.
BASE_WIDTH = 1000

# Tab cycles these. (label, kind, side) - kind/side of None means no narrowing.
# With the search box empty, a filter lists that whole category.
BASE_FILTERS = (
    ("All", None, None),
    ("Guns", "weapon", None),
    ("Ammo", "ammo", None),
    ("Mags", "magazine", None),
    ("Parts", "part", None),
    ("Gear", "gear", None),
    ("Meds", "med", None),
    ("Keys", "key", None),
    ("Barter", "barter", None),
)

# Short label drawn beside every result. Every item is classified at import,
# so no row is ever left blank.
KIND_TAGS = {
    "weapon": "GUN", "ammo": "AMO", "magazine": "MAG", "part": "PRT",
    "gear": "GEAR", "med": "MED", "key": "KEY", "barter": "BART",
    "food": "FOOD", "grenade": "NADE", "ammobox": "BOX", "container": "CONT",
    "knife": "BLDE", "map": "MAP", "money": "CASH", "special": "SPEC",
    "info": "INFO", "item": "ITEM",
    "extract": "EXT", "needed": "NEED", "have": "HAVE", "watch": "WCH",
    "recent": "↻",
}
# Only offered once a TarkovTracker account has been synced. The integration
# is optional, and an empty filter is worse than no filter.
TRACKER_FILTER = ("Needed", "needed", None)
# Personal lists, only offered once they hold something.
LIST_FILTERS = {"have": ("Have", "have", None), "watch": ("Watch", "watch", None)}
EXTRACT_FILTERS = (
    ("Extracts", "extract", None),
    ("Exfil PMC", "extract", "Pmc"),
    ("Exfil Scav", "extract", "Scav"),
    ("Exfil Co-op", "extract", "Coop"),
)


# Ctrl+<key> jumps straight to a chip. Digits run 1-9 then 0 for the tenth,
# the way browser tabs number themselves, and the home-row letters carry on
# from there. Anything past the fifteenth chip is still reachable with Tab.
FILTER_KEYS = "1234567890yuiop"


def filter_key(index: int) -> str:
    return FILTER_KEYS[index] if index < len(FILTER_KEYS) else ""


def apply_saved_order(filters: list, order: list[str]) -> list:
    """Reorder chips to a saved label order.

    Labels the saved order does not mention keep their relative position at
    the end, so a filter added in a later version still shows up rather than
    silently vanishing because an old config never listed it.
    """
    if not order:
        return filters
    by_label = {entry[0]: entry for entry in filters}
    ordered = [by_label.pop(label) for label in order if label in by_label]
    ordered.extend(entry for entry in filters if entry[0] in by_label)
    return ordered


def build_filters(conn) -> tuple:
    """The filter set this database can actually serve."""
    filters = list(BASE_FILTERS)
    if searchmod.tracker_configured(conn):
        filters.append(TRACKER_FILTER)
    for name, entry in LIST_FILTERS.items():
        if searchmod.listed(conn, name):
            filters.append(entry)
    try:
        has_extracts = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='extracts'"
        ).fetchone() and conn.execute("SELECT 1 FROM extracts LIMIT 1").fetchone()
    except Exception:
        has_extracts = False
    if has_extracts:
        filters.extend(EXTRACT_FILTERS)
    order = (load_config().get("search") or {}).get("filter_order") or []
    return tuple(apply_saved_order(filters, order))

# Penetration bands used for the colour cue, matching how players talk about
# armour class: roughly "handles class N".
PEN_BANDS = [(20, "#8a5a5a"), (30, "#8a7a4a"), (37, "#7a8a4a"), (45, "#5a8a5a"), (999, "#4a8a7a")]


SW_SHOW = 5
SW_RESTORE = 9
SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
SPIF_SENDCHANGE = 0x02
ASFW_ANY = -1


def _force_foreground(hwnd: int) -> bool:
    """Bring a window to the front, even over a focused fullscreen game.

    Windows only lets a process call SetForegroundWindow when it "owns" the
    foreground - which includes the case where it registered the hotkey the
    user just pressed. That covers normal use. The extra steps here are for
    the cases where it does not: attaching our input thread to the current
    foreground thread, and briefly zeroing the foreground lock timeout.
    """
    if not hwnd:
        return False
    try:
        user32.ShowWindow(hwnd, SW_SHOW)
        user32.BringWindowToTop(hwnd)

        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return True

        try:
            user32.AllowSetForegroundWindow(ASFW_ANY)
        except Exception:
            pass

        if user32.SetForegroundWindow(hwnd):
            return True

        # Attach to the foreground thread's input queue so we are considered
        # part of it, then try again.
        attached = False
        target_tid = 0
        our_tid = kernel32.GetCurrentThreadId()
        if fg:
            target_tid = user32.GetWindowThreadProcessId(fg, None)
            if target_tid and target_tid != our_tid:
                attached = bool(user32.AttachThreadInput(target_tid, our_tid, True))
        try:
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(target_tid, our_tid, False)

        if user32.GetForegroundWindow() == hwnd:
            return True

        # Last resort: drop the foreground lock timeout, retry, put it back.
        previous = wintypes.UINT(0)
        if user32.SystemParametersInfoW(
            SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(previous), 0
        ):
            user32.SystemParametersInfoW(
                SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(0), SPIF_SENDCHANGE
            )
            user32.SetForegroundWindow(hwnd)
            user32.SystemParametersInfoW(
                SPI_SETFOREGROUNDLOCKTIMEOUT,
                0,
                ctypes.c_void_p(previous.value),
                SPIF_SENDCHANGE,
            )

        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _fmt_price(value) -> str:
    return f"{value:,}" if isinstance(value, int) and value else "-"


def _price_col(row) -> str:
    """Trailing price column, omitted entirely when there is no price.

    A template-only database has no market data, so reserving the column
    would just push item names out of view for a row of dashes.
    """
    value = row.get("avg_24h_price")
    return f" {_fmt_price(value):>9}" if isinstance(value, int) and value else ""


def _refresh_prices(conn) -> bool:
    """Pull a fresh flea snapshot if the stored one has aged out.

    Wrapped so a missing module or a dead network never takes the overlay
    with it - out-of-date prices are worth more than a crash.
    """
    try:
        from . import prices as prices_mod

        return prices_mod.refresh_if_stale(conn)
    except Exception:
        return False


def _signed(value) -> str:
    """Modifier with an explicit sign, so +7 ergo reads differently from -7."""
    if value in (None, ""):
        return "-"
    number = float(value)
    if number == 0:
        return "0"
    return f"{number:+.0f}" if number == int(number) else f"{number:+.1f}"


def _pen_colour(pen) -> str:
    if not isinstance(pen, (int, float)):
        return FG
    for threshold, colour in PEN_BANDS:
        if pen < threshold:
            return colour
    return FG


class Popover:
    def __init__(self, conn: sqlite3.Connection, max_results: int = 40):
        self.conn = conn
        self.max_results = max_results
        self.results: list[dict] = []
        self.events: queue.Queue[str] = queue.Queue()
        self._visible = False
        self._drag_offset: tuple[int, int] | None = None
        self._last_position: tuple[int, int] | None = None
        self._size = (BASE_WIDTH, 620)
        self.filter_index = 0
        self.filters = build_filters(conn)
        self._arrow_mode = ((load_config().get('search') or {})
                            .get('arrow_keys_switch_filters') or 'always')
        self._syncing = False
        self._sync_lines: list[str] = []
        # Levels, each replacing the results list in place: 'search' shows
        # results, 'slots' one weapon's attachment categories with that
        # category's parts compared in the detail pane, 'parts' those parts
        # as rows of their own, and 'fits' the weapons a part goes on.
        # Enter descends, Esc comes back up through `back`.
        self.view = 'search'
        self.slot_weapon: dict | None = None
        self.slot_entries: list[dict] = []
        self.part_entries: list[dict] = []
        self.fits_entries: list[dict] = []
        self.back: list[dict] = []
        self.have: set[str] = set()
        self.watch: set[str] = set()
        self.have_qty: dict[str, int] = {}
        # Set while the search box is being used to type a stash count.
        self.qty_target: str | None = None
        self.qty_name = ""
        self.qty_prev_term = ""
        # 'search' types into the box, 'normal' treats keys as commands.
        # It opens in search mode so the hotkey is still followed by typing.
        self.mode = "search"
        self.vim_keys = bool(load_config()["search"].get("vim_keys", True))
        self.status_note = ''
        self._refresh_marks()
        self._build()
        threading.Thread(target=self._price_worker, daemon=True).start()

    # --- construction --------------------------------------------------

    def _build(self) -> None:
        self.root = tk.Tk()
        # The gamma watcher identifies this window by title + class so that
        # summoning the popover does not read as "focus left the game".
        # Keep this string in sync with gamma.DEFAULT_COMPANION_TITLES.
        self.root.title(WINDOW_TITLE)
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        width, height = self._size
        saved = load_config()["search"].get("position")
        if isinstance(saved, (list, tuple)) and len(saved) == 2:
            x, y = self._clamp_position(int(saved[0]), int(saved[1]))
        else:
            x, y = self._default_position()
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        mono = "Consolas" if "Consolas" in tkfont.families() else "Courier New"
        self.font_entry = (mono, 16)
        self.font_list = (mono, 11)
        self.font_detail = (mono, 11)

        outer = tk.Frame(self.root, bg=ACCENT, padx=1, pady=1)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=BG)
        inner.pack(fill="both", expand=True)

        # The window is overrideredirect, so it has no system title bar. This
        # strip stands in for one: it spans the full width and every pixel of
        # it drags, including the label, so the whole top edge is grabbable.
        self.titlebar = tk.Frame(inner, bg=TITLE_BG, height=26)
        self.titlebar.pack(fill="x", side="top")
        self.titlebar.pack_propagate(False)
        self.handle = tk.Label(
            self.titlebar,
            text="  ≡  " + WINDOW_TITLE,
            bg=TITLE_BG, fg=FG_DIM, font=(mono, 9), anchor="w",
        )
        self.handle.pack(side="left", fill="both", expand=True)
        self.drag_hint = tk.Label(
            self.titlebar,
            text="drag to move  ·  double-click to centre  ",
            bg=TITLE_BG, fg="#5a606c", font=(mono, 8), anchor="e",
        )
        self.drag_hint.pack(side="right", fill="y")
        # Bind every child too: a Frame only receives events on its own
        # exposed pixels, not those covered by a child widget.
        for widget in (self.titlebar, self.handle, self.drag_hint):
            self._make_draggable(widget)

        # search row
        row = tk.Frame(inner, bg=BG_ALT)
        row.pack(fill="x", padx=0, pady=0)
        tk.Label(
            row, text="  search ", bg=BG_ALT, fg=ACCENT, font=self.font_entry
        ).pack(side="left")
        self._make_draggable(row)
        self.entry = tk.Entry(
            row, bg=BG_ALT, fg=FG, insertbackground=ACCENT, font=self.font_entry,
            relief="flat", highlightthickness=0,
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 10))
        self.entry.bind("<KeyPress>", self._on_keypress)
        self.entry.bind("<KeyRelease>", self._on_type)
        self.entry.bind("<slash>", self._start_search)
        self.entry.bind("<Down>", self._nav_down)
        self.entry.bind("<Up>", self._nav_up)
        self.entry.bind("<Return>", self._nav_enter)
        self.entry.bind("<Control-Return>", self._open_wiki)
        self.entry.bind("<Control-Shift-Return>", self._open_market)
        self.entry.bind("<Escape>", self._on_escape)
        self.entry.bind("<BackSpace>", self._on_backspace)
        # Tab would otherwise move focus out of the entry, so both bindings
        # return "break". (ISO_Left_Tab is an X11 keysym and is not valid on
        # Windows Tk - Shift-Tab is the portable spelling.)
        self.entry.bind("<Control-h>", lambda e: self._toggle_list("have"))
        self.entry.bind("<Control-H>", self._ask_quantity)      # Ctrl+Shift+H
        self.entry.bind("<Control-Up>", lambda e: self._bump_quantity(1))
        self.entry.bind("<Control-Down>", lambda e: self._bump_quantity(-1))
        # Ctrl+Delete rather than Delete: the search box owns plain Delete.
        self.entry.bind("<Control-Delete>", self._remove_from_stash)
        self.entry.bind("<Control-d>", lambda e: self._toggle_list("watch"))
        for position, key in enumerate(FILTER_KEYS):
            self.entry.bind(f"<Control-Key-{key}>",
                            lambda e, n=position: self._jump_filter(n))
        self.entry.bind("<Left>", lambda e: self._arrow_filter(-1))
        self.entry.bind("<Right>", lambda e: self._arrow_filter(1))
        self.entry.bind("<Control-Shift-Left>", lambda e: self._move_filter(-1))
        self.entry.bind("<Control-Shift-Right>", lambda e: self._move_filter(1))
        self.entry.bind("<F5>", self._sync)
        self.entry.bind("<Control-r>", self._sync)
        self.entry.bind("<Tab>", self._next_filter)
        self.entry.bind("<Shift-Tab>", self._prev_filter)

        # filter chips
        self.filter_bar = tk.Frame(inner, bg=BG_ALT)
        self.filter_bar.pack(fill="x")
        self.filter_labels = []
        self.font_chip = (mono, 9)
        tk.Label(self.filter_bar, text="  Tab ", bg=BG_ALT, fg="#5a606c",
                 font=self.font_chip).pack(side="left")
        for index, (label, _kind, _side) in enumerate(self.filters):
            # Advertise the shortcut on the chip itself.
            key = filter_key(index)
            shown = f" {key} {label} " if key else f" {label} "
            widget = tk.Label(self.filter_bar, text=shown, bg=BG_ALT,
                              fg=FG_DIM, font=self.font_chip, padx=4)
            widget.pack(side="left")
            widget.bind("<Button-1>", lambda e, i=index: self._set_filter(i))
            self.filter_labels.append(widget)

        body = tk.Frame(inner, bg=BG)
        body.pack(fill="both", expand=True)

        self.listbox = tk.Listbox(
            body, bg=BG, fg=FG, font=self.font_list, relief="flat",
            highlightthickness=0, selectbackground=SEL, selectforeground=ACCENT,
            activestyle="none", width=40,
        )
        self.listbox.pack(side="left", fill="y", padx=(6, 0), pady=6)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.listbox.bind("<Escape>", self._on_escape)

        self.detail = tk.Text(
            body, bg=BG, fg=FG, font=self.font_detail, relief="flat",
            highlightthickness=0, wrap="none", padx=14, pady=8, state="disabled",
        )
        self.detail.pack(side="left", fill="both", expand=True, pady=6, padx=(6, 6))
        for name, colour in (
            ("head", ACCENT), ("dim", FG_DIM), ("label", "#9fb4d0"),
            ("good", "#6fbf8f"), ("warn", "#d0a05a"),
        ):
            self.detail.tag_configure(name, foreground=colour)
        for threshold, colour in PEN_BANDS:
            self.detail.tag_configure(f"pen{threshold}", foreground=colour)

        hintbar = tk.Frame(inner, bg=BG_ALT)
        hintbar.pack(fill="x", side="bottom")
        # Which mode you are in has to be visible: the same keypress either
        # types a letter or jumps a row, and guessing which is unpleasant.
        self.mode_label = tk.Label(hintbar, text="", bg=BG_ALT, fg=FG_DIM,
                                   font=(mono, 9, "bold"))
        self.mode_label.pack(side="left")
        hint = tk.Label(
            hintbar,
            text="  j k move   / search   Enter open   h back   Tab filter   Ctrl+Enter wiki   Ctrl+Shift+Enter price   Ctrl+H have   Ctrl+Shift+H count   Ctrl+Del drop   Ctrl+D watch   :help  ",
            bg=BG_ALT, fg=FG_DIM, font=(mono, 9), anchor="w",
        )
        hint.pack(fill="x", side="left", expand=True)
        self._make_draggable(hint)
        self._make_draggable(hintbar)
        self._render_mode()

        self.root.bind("<Escape>", self._on_escape)
        self.root.bind("<F5>", self._sync)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self._set_filter(0)
        self.root.update_idletasks()
        self._fit_to_filters()
        self.root.withdraw()
        self.root.after(60, self._pump)

    # --- dragging ------------------------------------------------------

    def _make_draggable(self, widget) -> None:
        widget.configure(cursor="fleur")
        widget.bind("<ButtonPress-1>", self._start_drag)
        widget.bind("<B1-Motion>", self._on_drag)
        widget.bind("<ButtonRelease-1>", self._end_drag)
        widget.bind("<Double-Button-1>", self._centre)

    def _default_position(self) -> tuple[int, int]:
        """Centred horizontally on the primary screen, a third of the way down."""
        width, height = self._size
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        return (sw - width) // 2, (sh - height) // 3

    def _clamp_position(self, x: int, y: int) -> tuple[int, int]:
        """Keep the window reachable across the whole multi-monitor desktop.

        The virtual desktop origin is negative when a second monitor sits left
        of or above the primary, so this clamps against that box rather than a
        single screen. A margin is kept on-screen so the drag handle can always
        be grabbed again.
        """
        width, height = self._size
        vx, vy, vw, vh = virtual_screen_bounds()
        margin = 120
        x = max(vx - width + margin, min(x, vx + vw - margin))
        y = max(vy, min(y, vy + vh - margin))
        return int(x), int(y)

    def _start_drag(self, event) -> None:
        self._drag_offset = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )

    def _on_drag(self, event) -> None:
        if self._drag_offset is None:
            return
        dx, dy = self._drag_offset
        x, y = self._clamp_position(event.x_root - dx, event.y_root - dy)
        self.root.geometry(f"+{x}+{y}")
        # Remember what we asked for. winfo_x/y lag until Tk processes the
        # geometry change, so reading them back on release can persist a
        # stale position.
        self._last_position = (x, y)

    def _end_drag(self, event=None) -> None:
        if self._drag_offset is None:
            return
        self._drag_offset = None
        if self._last_position is not None:
            self._save_position(*self._last_position)
            self._last_position = None
        else:
            self._save_position()

    def _centre(self, event=None) -> str:
        """Double-click the chrome to bring the window back to the middle."""
        self._drag_offset = None
        x, y = self._default_position()
        self.root.geometry(f"+{x}+{y}")
        # Save the intended coordinates: winfo_x/y still report the old
        # position until Tk has processed the geometry change.
        self._save_position(x, y)
        return "break"

    def _save_position(self, x: int | None = None, y: int | None = None) -> None:
        try:
            if x is None or y is None:
                x, y = self.root.winfo_x(), self.root.winfo_y()
            set_local_override("search", "position", [int(x), int(y)])
        except Exception:
            # Remembering the position is a convenience; never let it break the UI.
            pass

    # --- visibility ----------------------------------------------------

    def show(self) -> None:
        self._visible = True
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        # winfo_id() is a TkChild with no title; the window Windows actually
        # focuses (and that the gamma watcher identifies by title) is its
        # GA_ROOT ancestor.
        _force_foreground(toplevel_of(self.root.winfo_id()))
        self._set_mode("search")
        self.entry.focus_force()
        self.entry.select_range(0, "end")

    def hide(self) -> None:
        self._visible = False
        self.root.withdraw()

    def toggle(self) -> None:
        self.hide() if self._visible else self.show()

    def request_toggle(self) -> None:
        """Thread-safe: called from the hotkey listener thread."""
        self.events.put("toggle")

    def _reprice(self) -> None:
        """Redraw the current detail now that prices have changed."""
        self.status_note = "flea prices updated"
        try:
            if self.view == "search":
                entry = self._current_result()
                if entry:
                    self._render(entry["id"])
            else:
                subject = self._current_subject()
                if subject:
                    self._render(subject["id"])
        except Exception:
            pass

    # --- modes ---------------------------------------------------------

    def _set_mode(self, mode: str) -> None:
        """Switch between typing and commanding.

        Focus never moves: the entry keeps it in both modes and normal-mode
        keys are swallowed before they reach it. Moving focus to the list
        instead would mean Tk deciding which widget owns Tab, Escape and the
        arrow keys, which is exactly the argument this avoids.
        """
        if mode == self.mode:
            return
        self.mode = mode
        if mode == "search":
            self.entry.configure(insertwidth=2)
        else:
            self.entry.configure(insertwidth=0)   # no caret when not typing
        self._render_mode()

    def _render_mode(self) -> None:
        if not self.vim_keys:
            self.mode_label.configure(text="")
            return
        searching = self.mode == "search"
        self.mode_label.configure(
            text=" SEARCH " if searching else " NORMAL ",
            bg="#3a6ea5" if searching else "#4a4f5a",
            fg="#e8eaed",
        )

    def _start_search(self, event=None, keep: bool = False) -> str:
        """Focus the box for typing. '/' starts fresh, 'i' keeps what is there."""
        self._set_mode("search")
        if not keep:
            self.entry.delete(0, "end")
            self._on_type()
        self.entry.focus_set()
        return "break"

    NORMAL_KEYS = {
        "j": "down", "k": "up",
        "g": "first", "G": "last",
        "l": "enter", "h": "back",
        "q": "hide",
        "/": "search", "i": "search-keep", "a": "search-keep",
    }

    def _on_keypress(self, event=None):
        """In normal mode a bare key is a command, not a character.

        Returning "break" stops it reaching the entry, so the search box never
        fills up with navigation keys. Anything with a modifier is left alone -
        Ctrl+H and friends work identically in both modes.
        """
        if not self.vim_keys or self.mode != "normal" or self.qty_target:
            return None
        if event.state & 0x0004:          # Ctrl held: not ours to interpret
            return None

        action = self.NORMAL_KEYS.get(event.char)
        if action == "down":
            self._move(1)
        elif action == "up":
            self._move(-1)
        elif action == "first":
            self._jump_to(0)
        elif action == "last":
            self._jump_to(10 ** 6)
        elif action == "enter":
            self._nav_enter()
        elif action == "back":
            self._go_back()
        elif action == "hide":
            self.hide()
        elif action == "search":
            self._start_search()
        elif action == "search-keep":
            self._start_search(keep=True)
        elif event.char.isdigit():
            # Bare digits jump filters, the way Ctrl+digit does while typing.
            index = FILTER_KEYS.find(event.char)
            if index >= 0:
                self._jump_filter(index)
        else:
            return "break"   # swallow anything else rather than typing it
        return "break"

    def _jump_to(self, index: int) -> None:
        rows = {
            "slots": self.slot_entries,
            "parts": self.part_entries,
            "fits": self.fits_entries,
        }.get(self.view, self.results)
        if not rows:
            return
        index = max(0, min(len(rows) - 1, index))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.see(index)
        if self.view == "slots":
            self._render_slot(index)
        else:
            self._render(rows[index]["id"])

    def _pump(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event == "toggle":
                    self.toggle()
                elif isinstance(event, tuple):
                    kind, payload = event
                    if kind == "sync-status":
                        self._sync_lines.append(str(payload))
                        self._render_sync(done=False)
                    elif kind == "sync-done":
                        self._sync_finished()
                    elif kind == "sync-failed":
                        self._sync_finished(error=str(payload))
                    elif kind == "prices-updated":
                        self._reprice()
        except queue.Empty:
            pass
        self.root.after(60, self._pump)

    # --- filters -------------------------------------------------------

    # --- personal lists -------------------------------------------------

    def _current_row(self) -> int:
        """The highlighted row, or the first one when nothing is selected."""
        current = self.listbox.curselection()
        return current[0] if current else 0

    def _current_result(self) -> dict | None:
        """The highlighted search result, if the selection points at one.

        The selected row is not always a result. Browsing a weapon fills the
        same listbox with its slots and then their parts, and narrowing a
        search can leave the selection pointing past the end of the new,
        shorter list - so the index has to be checked rather than trusted.
        """
        if self.view != "search":
            return None
        index = self._current_row()
        return self.results[index] if index < len(self.results) else None

    def _row_in(self, rows: list[dict]) -> dict | None:
        """The highlighted entry of a browsing list, if the index is in range."""
        index = self._current_row()
        return rows[index] if index < len(rows) else None

    def _current_subject(self) -> dict | None:
        """The item the highlighted row stands for, in a browsing view."""
        if self.view == "fits":
            return self._row_in(self.fits_entries)
        if self.view == "parts":
            return self._row_in(self.part_entries)
        if self.view == "slots":
            # The rows here are categories, not parts, so the weapon is what
            # the marks apply to.
            return self.slot_weapon
        return None

    def _selected_item_id(self) -> str | None:
        """The item currently highlighted, whichever level is showing."""
        if self.view != "search":
            return (self._current_subject() or {}).get("id")
        entry = self._current_result()
        if not entry:
            return None
        item_id = entry.get("id") or ""
        return None if item_id.startswith("extract:") else item_id

    def _ask_quantity(self, event=None) -> str:
        """Turn the search box into "how many?" for the highlighted item.

        A prompt rather than a key you tap N times: counting out twelve
        Salewas one keypress at a time is not what you want mid-raid.
        """
        item_id = self._selected_item_id()
        if not item_id:
            return "break"
        self.qty_target = item_id
        self.qty_name = (
            (self._current_subject() or self._current_result() or {}).get("name") or ""
        ).replace("[DEMO] ", "")
        # The box is about to hold digits, so park the search term - otherwise
        # saving a count silently throws away the results behind the prompt.
        self.qty_prev_term = self.entry.get()
        self.entry.delete(0, "end")
        self._render_quantity_prompt()
        return "break"

    def _render_quantity_prompt(self) -> None:
        held = self.have_qty.get(self.qty_target or "", 0)
        typed = self.entry.get().strip()
        self._set_detail([
            (f"{self.qty_name}\n\n", "head"),
            ("  how many do you have?  ", "label"),
            (f"{typed or '_'}\n\n", "good"),
            (f"  currently holding {held}\n" if held else "  not in your stash yet\n", "dim"),
            ("\n  Enter to save, 0 to remove, Esc to cancel\n", "dim"),
        ])

    def _commit_quantity(self) -> str:
        typed = self.entry.get().strip()
        item_id, name = self.qty_target, self.qty_name
        self.qty_target = None
        self._restore_term()
        if not item_id or not typed.isdigit():
            self._on_type()
            return "break"
        held = searchmod.set_quantity(self.conn, item_id, "have", int(typed))
        self._refresh_marks()
        self._rebuild_filter_bar()
        self.status_note = (f"holding {held}" if held else f"removed {name}")
        self._redraw_current()
        return "break"

    def _cancel_quantity(self) -> str:
        self.qty_target = None
        self._restore_term()
        self._on_type()
        return "break"

    def _restore_term(self) -> None:
        """Put the search term back after the box was borrowed for a count."""
        self.entry.delete(0, "end")
        if self.qty_prev_term:
            self.entry.insert(0, self.qty_prev_term)
        self.qty_prev_term = ""

    def _bump_quantity(self, delta: int) -> str:
        """Nudge the count without leaving the results."""
        item_id = self._selected_item_id()
        if not item_id:
            return "break"
        held = searchmod.adjust_quantity(self.conn, item_id, "have", delta)
        self._refresh_marks()
        self._rebuild_filter_bar()
        self.status_note = f"holding {held}" if held else "removed from have"
        self._redraw_current()
        return "break"

    def _redraw_current(self) -> None:
        """Redraw whichever list is showing, keeping the selection."""
        keep = self._current_row()
        if self.view == "parts":
            self._show_parts(keep)
        elif self.view == "fits":
            self._show_fits(keep)
        elif self.view == "slots":
            self._render_slot(keep)
        else:
            self._on_type()
            if keep < self.listbox.size():
                self.listbox.selection_clear(0, "end")
                self.listbox.selection_set(keep)
                entry = self._current_result()
                if entry:
                    self._render(entry["id"])

    def _toggle_list(self, list_name: str) -> str:
        item_id = self._selected_item_id()
        if not item_id:
            return "break"
        now_listed = searchmod.toggle_list(self.conn, item_id, list_name)
        self._refresh_marks()
        # The chip only exists while the list has something in it.
        self._rebuild_filter_bar()
        if self.view == "fits":
            self._show_fits(self._current_row())
        elif self.view == "parts":
            self._show_parts(self._current_row())
        elif self.view == "slots":
            self._render_slot(self._current_row())
        else:
            # Redraw the rows so the marker appears, keeping the selection.
            keep = self.listbox.curselection()
            self._on_type()
            if keep and keep[0] < self.listbox.size():
                self.listbox.selection_clear(0, "end")
                self.listbox.selection_set(keep[0])
            self._render(item_id)
        verb = "added to" if now_listed else "removed from"
        self.status_note = f"{verb} {list_name}"
        return "break"

    def _refresh_marks(self) -> None:
        self.have = searchmod.listed(self.conn, "have")
        self.watch = searchmod.listed(self.conn, "watch")
        self.have_qty = searchmod.quantities(self.conn, "have")

    def _marks(self, item_id: str) -> str:
        """The star/diamond for a row, with the count when you hold more than one."""
        marks = ""
        if item_id in self.have:
            marks += MARK_HAVE
            count = self.have_qty.get(item_id, 1)
            if count > 1:
                marks += str(count)
        if item_id in self.watch:
            marks += MARK_WATCH
        return marks

    # --- slot browsing --------------------------------------------------

    def _descend(self) -> None:
        """Remember this level so Esc comes back to exactly it.

        The whole list is snapshotted, not just the row: browsing can loop -
        a part's weapons, one of those weapons' slots, another part - so the
        rows a level held cannot be recomputed from where you ended up.
        """
        self.back.append({
            "view": self.view,
            "index": self._current_row(),
            "slot_weapon": self.slot_weapon,
            "slot_entries": self.slot_entries,
            "part_entries": self.part_entries,
            "fits_entries": self.fits_entries,
        })

    def _go_back(self) -> str:
        """Step back up one level, or hide when already at the top."""
        if not self.back:
            self.hide()
            return "break"
        frame = self.back.pop()
        self.slot_weapon = frame["slot_weapon"]
        self.slot_entries = frame["slot_entries"]
        self.part_entries = frame["part_entries"]
        self.fits_entries = frame["fits_entries"]
        view, index = frame["view"], frame["index"]
        if view == "slots":
            self._show_slots(index)
        elif view == "parts":
            self._show_parts(index)
        elif view == "fits":
            self._show_fits(index)
        else:
            # Back at the results, which are re-run rather than stored - the
            # database may have changed underneath (a mark, a sync).
            self.view = "search"
            self._on_type()
            if index < self.listbox.size():
                self.listbox.selection_clear(0, "end")
                self.listbox.selection_set(index)
                entry = self._current_result()
                if entry:
                    self._render(entry["id"])
        return "break"

    def _enter_slots(self, weapon: dict) -> None:
        """Show a weapon's attachment categories in place of the results."""
        slots = searchmod.weapon_slots(self.conn, weapon["id"])
        if not slots:
            return
        self._descend()
        self.slot_weapon = weapon
        self.slot_entries = slots
        self._show_slots(0)

    def _show_slots(self, index: int = 0) -> None:
        """Draw the categories, highlighting one.

        Coming back from a category's parts returns to that category rather
        than to the top of the list, so backing out of a rabbit hole leaves
        you where you were.
        """
        self.view = "slots"
        self.part_entries = []
        self.listbox.delete(0, "end")
        for entry in self.slot_entries:
            self.listbox.insert("end", f"{entry['count']:>3}  {entry['label']}")
        index = max(0, min(len(self.slot_entries) - 1, index))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.see(index)
        self._render_slot(index)

    def _enter_parts(self, index: int) -> None:
        """Drill into one category, listing its parts as rows.

        The category view compares every part at once, which answers "which
        of these is best". This answers "what about that one" - each part
        gets the full detail pane, and can be marked or opened on the wiki
        like any other result.
        """
        if index >= len(self.slot_entries):
            return
        entry = self.slot_entries[index]
        parts = searchmod.parts_for_slot(
            self.conn, self.slot_weapon["id"], entry["slot"]
        )
        if not parts:
            return
        self._descend()
        self.part_entries = parts
        self._show_parts(0)

    def _show_parts(self, index: int = 0) -> None:
        """Draw the current category's parts, highlighting one."""
        self.view = "parts"
        self.listbox.delete(0, "end")
        for part in self.part_entries:
            mark = self._marks(part.get("id") or "")
            name = (part.get("name") or "").replace("[DEMO] ", "")
            # Ergo earns the space a kind tag would take: every row here is a
            # part, and the list is ordered by it.
            self.listbox.insert(
                "end", f"{_signed(part.get('ergonomics')):>4} {mark:<4}{name[:28]}"
            )
        index = max(0, min(len(self.part_entries) - 1, index))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.see(index)
        self._render(self.part_entries[index]["id"])

    def _enter_fits(self) -> None:
        """List the weapons a part goes on, as rows to walk through.

        The detail pane can only fit the first handful before it runs out of
        room, and a common part fits dozens of guns.
        """
        item_id = self._selected_item_id()
        if not item_id:
            return
        fits = (searchmod.describe(self.conn, item_id) or {}).get("fits") or []
        if not fits:
            return
        self._descend()
        self.fits_entries = fits
        self._show_fits(0)

    def _show_fits(self, index: int = 0) -> None:
        """Draw the weapons the current part fits, highlighting one."""
        self.view = "fits"
        self.listbox.delete(0, "end")
        for gun in self.fits_entries:
            mark = self._marks(gun.get("id") or "")
            name = (gun.get("name") or "").replace("[DEMO] ", "")
            self.listbox.insert("end", f"{'GUN':<4} {mark:<4}{name[:28]}")
        index = max(0, min(len(self.fits_entries) - 1, index))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.see(index)
        self._render(self.fits_entries[index]["id"])

    def _render_slot(self, index: int) -> None:
        entry = self.slot_entries[index]
        weapon = self.slot_weapon
        parts = searchmod.parts_for_slot(self.conn, weapon["id"], entry["slot"])
        name = (weapon.get("name") or "").replace("[DEMO] ", "")
        out: list[tuple[str, str]] = [
            (f"{name}\n", "head"),
            (f"  {entry['label']} - {entry['count']} options\n\n", "dim"),
            ("    ergo  recoil   loud  part\n", "dim"),
        ]
        for part in parts:
            ergo = part.get("ergonomics")
            recoil = part.get("recoil_modifier")
            loud = part.get("loudness")
            out.append((f"  {_signed(ergo):>6}", "good" if (ergo or 0) > 0 else "dim"))
            out.append((f"  {_signed(recoil):>6}", "good" if (recoil or 0) < 0 else "dim"))
            out.append((f"  {_signed(loud):>5}", "dim"))
            mark = self._marks(part.get("id") or "")
            out.append((f"  {mark:<4}", "good" if mark else "dim"))
            out.append((f"{(part['name'] or '')[:50]}\n", None))
        if not parts:
            out.append(("    no parts recorded\n", "dim"))
        if parts:
            out.append(("\n  Enter lists these as rows you can mark or open.\n", "dim"))
        out.append(("  Esc or Backspace goes back.\n", "dim"))
        self._set_detail(out)

    # --- syncing -------------------------------------------------------

    def _sync(self, event=None) -> str:
        """Refresh TarkovTracker progress without leaving the overlay."""
        if self._syncing:
            return "break"
        from . import tracker as tracker_mod

        if not tracker_mod.load_token():
            self._set_detail([
                ("No TarkovTracker account connected.\n\n", "head"),
                ("This is optional - everything else works without it.\n"
                 "To connect one:\n\n", "dim"),
                ("  uv run tarkov-tools tracker login --token PVP_xxxxx\n", "label"),
            ])
            return "break"

        self._syncing = True
        self._sync_lines = ["Syncing with TarkovTracker ...\n"]
        self._render_sync(done=False)
        threading.Thread(target=self._sync_worker, daemon=True).start()
        return "break"

    def _price_worker(self) -> None:
        """Pull a fresh flea snapshot in the background at startup.

        Prices are the one thing here that goes stale by the hour, and the
        answer wanted mid-raid is what something sells for *now*. Its own
        connection, because SQLite connections belong to the thread that
        made them, and silent because a failure just leaves the last
        snapshot in place.
        """
        from . import db as dbmod

        try:
            conn = dbmod.connect()
            try:
                if _refresh_prices(conn):
                    self.events.put(("prices-updated", None))
                # The slug map only changes when the game patches, but pull it
                # here so the first Ctrl+Shift+Enter is not a network wait.
                try:
                    from . import market as market_mod

                    market_mod.refresh_if_stale(conn)
                except Exception:
                    pass
            finally:
                conn.close()
        except Exception:
            pass

    def _sync_worker(self) -> None:
        """Runs off the main thread; talks to the UI only through the queue.

        Opens its own database connection: SQLite connections belong to the
        thread that created them, and the popover's belongs to the Tk thread.
        """
        from . import db as dbmod
        from . import tracker as tracker_mod

        try:
            conn = dbmod.connect()
            try:
                with conn:
                    tracker_mod.sync_needed(
                        conn, verbose=False,
                        on_status=lambda m: self.events.put(("sync-status", m)),
                    )
                self.events.put(("sync-status", "flea prices"))
                _refresh_prices(conn)
            finally:
                conn.close()
            self.events.put(("sync-done", None))
        except Exception as exc:
            self.events.put(("sync-failed", str(exc)))

    def _render_sync(self, done: bool, error: str | None = None) -> None:
        chunks: list[tuple[str, str]] = [
            ("TarkovTracker sync\n\n", "head")
        ]
        for line in self._sync_lines:
            chunks.append((line if line.endswith("\n") else line + "\n", "dim"))
        if error:
            chunks.append((f"\nfailed: {error}\n", "warn"))
        elif done:
            chunks.append(("\ndone - press Tab to the Needed filter\n", "good"))
        self._set_detail(chunks)

    def _sync_finished(self, error: str | None = None) -> None:
        self._syncing = False
        self._render_sync(done=error is None, error=error)
        if error:
            return
        # A first successful sync makes the Needed filter available, so the
        # chips are rebuilt rather than left stale.
        self._rebuild_filter_bar()

    def _rebuild_filter_bar(self) -> None:
        current = self.filters[self.filter_index][0] if self.filters else "All"
        self.filters = build_filters(self.conn)
        for widget in self.filter_labels:
            widget.destroy()
        self.filter_labels = []
        for index, (label, _kind, _side) in enumerate(self.filters):
            # Advertise the shortcut on the chip itself.
            key = filter_key(index)
            shown = f" {key} {label} " if key else f" {label} "
            widget = tk.Label(self.filter_bar, text=shown, bg=BG_ALT,
                              fg=FG_DIM, font=self.font_chip, padx=4)
            widget.pack(side="left")
            widget.bind("<Button-1>", lambda e, i=index: self._set_filter(i))
            self.filter_labels.append(widget)
        names = [f[0] for f in self.filters]
        self.filter_index = names.index(current) if current in names else 0
        self._highlight_filters()
        self._fit_to_filters()

    def _fit_to_filters(self) -> None:
        """Widen the window so the whole filter bar is visible.

        The chips are laid out left to right with no wrapping, so with enough
        of them the last ones are clipped or pushed off the edge entirely. The
        natural width is measured from what the chips ask for rather than
        guessed, and only ever grows the window - never shrinks it below the
        base size, and never past the monitor it sits on.
        """
        configured = (load_config().get("search") or {}).get("width")
        try:
            needed = sum(w.winfo_reqwidth() for w in self.filter_bar.winfo_children())
        except Exception:
            return
        # A little slack so the last chip is not flush against the border.
        wanted = int(configured) if configured else needed + 24
        width, height = self._size
        screen = self.root.winfo_screenwidth()
        wanted = max(BASE_WIDTH, min(wanted, screen - 40))
        if wanted == width:
            return
        self._size = (wanted, height)
        x, y = self._clamp_position(self.root.winfo_x(), self.root.winfo_y())
        self.root.geometry(f"{wanted}x{height}+{x}+{y}")

    def _highlight_filters(self) -> None:
        for position, widget in enumerate(self.filter_labels):
            active = position == self.filter_index
            widget.configure(fg=BG if active else FG_DIM,
                             bg=ACCENT if active else BG_ALT)

    def _set_filter(self, index: int) -> str:
        self.filter_index = index % len(self.filters)
        self._highlight_filters()
        self._on_type()
        return "break"

    def _jump_filter(self, number: int) -> str:
        """Jump to a chip by position; see FILTER_KEYS for the key order."""
        if number < len(self.filters):
            self._set_filter(number)
        return "break"

    def _move_filter(self, delta: int) -> str:
        """Slide the active chip along the bar and remember the new order."""
        target = self.filter_index + delta
        if not 0 <= target < len(self.filters):
            return "break"
        # Lift and reinsert rather than swap: identical for a single step, and
        # correct if this is ever called with a larger delta.
        order = [entry[0] for entry in self.filters]
        order.insert(target, order.pop(self.filter_index))
        try:
            set_local_override("search", "filter_order", order)
        except Exception:
            pass  # reordering is a convenience; never let it break the UI
        self.filters = tuple(apply_saved_order(list(self.filters), order))
        self.filter_index = target
        self._rebuild_filter_bar()
        return "break"

    def _arrow_filter(self, delta: int) -> str | None:
        """Left/Right move between filters, subject to the configured mode.

        Returning None lets Tk handle the key normally, which is what keeps
        ordinary caret movement working in "edges" and "never" modes.
        """
        mode = self._arrow_mode
        if mode == "never":
            return None
        if mode == "edges":
            try:
                caret = self.entry.index("insert")
                length = len(self.entry.get())
            except Exception:
                caret, length = 0, 0
            at_edge = (caret == 0) if delta < 0 else (caret >= length)
            if not at_edge:
                return None
        return self._set_filter(self.filter_index + delta)

    def _next_filter(self, event=None) -> str:
        return self._set_filter(self.filter_index + 1)

    def _prev_filter(self, event=None) -> str:
        return self._set_filter(self.filter_index - 1)

    # --- search --------------------------------------------------------

    HELP = [
        ("Tarkov Tools\n\n", "head"),

        ("  two modes\n", "head"),
        ("    SEARCH          ", "label"), ("typing goes in the box\n", None),
        ("    NORMAL          ", "label"), ("keys are commands - j k move, no typing\n", None),
        ("    Esc             ", "label"), ("leave SEARCH for NORMAL, then back out\n", None),
        ("    /               ", "label"), ("start a fresh search\n", None),
        ("    i               ", "label"), ("keep typing on what is already there\n", None),
        ("    The mode shows bottom-left. Set search.vim_keys false in\n", "dim"),
        ("    config.json to turn all of this off.\n", "dim"),

        ("\n  moving about\n", "head"),
        ("    j k  /  Up Down ", "label"), ("next, previous\n", None),
        ("    g G             ", "label"), ("first, last\n", None),
        ("    Enter  /  l     ", "label"), ("open what is highlighted\n", None),
        ("    h  /  Backspace ", "label"), ("back one level\n", None),
        ("    q               ", "label"), ("hide the window\n", None),
        ("    Tab  /  1-0 y-p ", "label"), ("switch filter (Ctrl+key while typing)\n", None),

        ("\n  going deeper (Enter)\n", "head"),
        ("    on a gun        ", "label"), ("its attachment categories\n", None),
        ("    on a category   ", "label"), ("the parts that fit, best ergo first\n", None),
        ("    on a part       ", "label"), ("every weapon it goes on\n", None),
        ("    on an extract   ", "label"), ("the wiki map, zoomed out, exit marked\n", None),

        ("\n  opening things\n", "head"),
        ("    Ctrl+Enter      ", "label"), ("the wiki page\n", None),
        ("    Ctrl+Shift+Enter ", "label"), ("the flea market price page\n", None),

        ("\n  your inventory\n", "head"),
        ("    Ctrl+H          ", "label"), ("mark one as in your stash\n", None),
        ("    Ctrl+Shift+H    ", "label"), ("type how many you have\n", None),
        ("    Ctrl+Up Down    ", "label"), ("add or remove one\n", None),
        ("    Ctrl+Del        ", "label"), ("take it out of your stash\n", None),
        ("    to see it all   ", "label"), ("Tab to the Have filter, or Ctrl+its number\n", None),
        ("    The Have chip appears in the filter bar once you hold\n", "dim"),
        ("    something, listed by what each pile is worth, with a total.\n", "dim"),
        ("    In a terminal: uv run tarkov-tools stash\n", "dim"),

        ("\n  things to look out for\n", "head"),
        ("    Ctrl+D          ", "label"), ("mark as worth watching for\n", None),
        ("    to see it all   ", "label"), ("Tab to the Watch filter\n", None),
        ("    Watched items carry a diamond everywhere they appear,\n", "dim"),
        ("    including inside a gun's parts list.\n", "dim"),

        ("\n  keeping current\n", "head"),
        ("    F5              ", "label"), ("refresh prices, and TarkovTracker if connected\n", None),
        ("    Flea prices refresh on their own, hourly at source.\n", "dim"),
        ("    'not on the flea market' means banned, not missing.\n", "dim"),
        ("    Value per slot is left off weapons - a built gun's size\n", "dim"),
        ("    depends on its attachments.\n", "dim"),

        ("\n  odds and ends\n", "head"),
        ("    empty box       ", "label"), ("shows what you searched recently\n", None),
        ("    drag the top    ", "label"), ("move the window anywhere\n", None),
        ("    :help           ", "label"), ("this, any time\n", None),
    ]

    def _on_type(self, event=None) -> None:
        if event is not None and event.keysym in ("Up", "Down", "Return", "Escape",
                                                  "Tab", "ISO_Left_Tab"):
            return
        if self.view != "search":
            # Typing means the user wants to search again, however deep in.
            self.view = "search"
            self.back = []
            self.slot_weapon = None
            self.slot_entries = []
            self.part_entries = []
            self.fits_entries = []
        if self.qty_target:
            # The box is collecting a count, not a search term.
            self._render_quantity_prompt()
            return
        if event is not None and self.vim_keys and self.mode == "normal":
            return   # the keypress was a command; the term did not change
        term = self.entry.get().strip()
        if term.lower() in (":help", ":h", ":?"):
            self.results = []
            self.listbox.delete(0, "end")
            self._set_detail(list(self.HELP))
            return
        _label, kind, side = self.filters[self.filter_index]
        # An active filter with an empty box lists that whole category, so the
        # filter doubles as a way to browse.
        if term or kind:
            self.results = searchmod.search(
                self.conn, term, self.max_results, kind=kind, side=side
            )
        else:
            # Nothing typed and no filter: offer what you looked up before,
            # which beats an empty pane you have to type your way out of.
            self.results = searchmod.recent_searches(self.conn)
        self.listbox.delete(0, "end")
        for r in self.results:
            tag = KIND_TAGS.get(r["kind"], "ITEM")
            mark = self._marks(r.get("id") or "")
            name = (r["name"] or "").replace("[DEMO] ", "")
            if r["kind"] == "needed" and r.get("short_name"):
                name = f"{name} · {r['short_name']}"
            elif r["kind"] == "extract" and r.get("short_name"):
                # Two extracts can share a name (a PMC and a Scav one), so the
                # map is shown here and the side in the detail pane.
                name = f"{name} · {r['short_name']}"
            self.listbox.insert("end", f"{tag:<4} {mark:<4}{name[:28]}")
        if self.results:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(0)
            self._render(self.results[0]["id"])
            if kind == "have" and not term:
                self._append_detail(self._stash_footer())
        else:
            self._set_detail([("no matches\n", "dim")] if term else
                             [("type a gun, round, magazine or extract name\n", "dim"),
                              ("or :help for the keys\n", "dim")])

    def _on_escape(self, event=None) -> str:
        """Escape leaves typing first, then backs out a level, then hides."""
        if self.qty_target:
            return self._cancel_quantity()
        if self.vim_keys and self.mode == "search" and self.listbox.size():
            # Something to browse: drop into normal mode rather than leaving.
            self._set_mode("normal")
            return "break"
        return self._go_back()

    def _on_backspace(self, event=None):
        # Only intercept backspace when it cannot be editing the search text,
        # otherwise it would stop deleting characters.
        if self.view != "search" and not self.entry.get():
            return self._go_back()
        return None

    def _move(self, delta: int) -> None:
        rows = {
            "slots": self.slot_entries,
            "parts": self.part_entries,
            "fits": self.fits_entries,
        }.get(self.view, self.results)
        if not rows:
            return
        index = max(0, min(len(rows) - 1, self._current_row() + delta))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.see(index)
        if self.view == "slots":
            self._render_slot(index)
        else:
            self._render(rows[index]["id"])

    def _nav_down(self, event=None):
        self._move(1)
        return "break"

    def _nav_up(self, event=None):
        self._move(-1)
        return "break"

    def _open_extract(self, result_id: str) -> None:
        """Hide the popover, then open the interactive map on this extract.

        The popover must be hidden first: reusing an existing tab types the
        URL with Ctrl+L, and those keystrokes would otherwise land here.
        """
        from . import extracts as extracts_mod

        data = searchmod.describe(self.conn, result_id)
        extract = (data or {}).get("extract")
        if not extract:
            return
        url = extracts_mod.wiki_url(extract)
        reuse = (
            extracts_mod.map_title(extract["wiki_page"])
            if extract.get("wiki_page")
            else None
        )
        self.hide()
        self.root.update_idletasks()
        try:
            opened, _ = extracts_mod.open_in_browser(url, reuse_title=reuse)
            cfg = load_config().get("extracts", {})
            if opened and cfg.get("apply_map_filters", True) and extract.get("wiki_page"):
                extracts_mod.prepare_map(
                    extract["wiki_page"],
                    cfg.get("categories"),
                    fullscreen=cfg.get("fullscreen_map", True),
                    unzoom=cfg.get("zoom_out_map", True),
                    marker_name=extract.get("display_name"),
                )
        except Exception as exc:
            print(f"could not open the map: {exc}")

    def _wiki_name(self) -> str | None:
        """What the highlighted row should be looked up as on the wiki.

        An extract goes to its map's article rather than the extract itself -
        the wiki has a page per location, not per exit, and plain Enter
        already opens the interactive map on the exit.
        """
        if self.view != "search":
            return (self._current_subject() or {}).get("name")
        entry = self._current_result()
        if not entry:
            return None
        if entry.get("kind") == "extract":
            return entry.get("short_name") or entry.get("name")
        return entry.get("name")

    def _market_name(self) -> str | None:
        """The item to price up. Extracts have no market page, so they opt out."""
        if self.view != "search":
            return (self._current_subject() or {}).get("name")
        entry = self._current_result()
        if not entry or entry.get("kind") == "extract":
            return None
        return entry.get("name")

    def _open_market(self, event=None):
        """Open the highlighted item's flea market page.

        Reuses any tarkovforge tab rather than opening one per lookup - in a
        raid this gets pressed repeatedly, and a pile of tabs is its own
        problem.
        """
        from . import extracts as extracts_mod
        from . import market as market_mod

        name = (self._market_name() or "").replace("[DEMO] ", "").strip()
        if not name:
            return "break"
        searchmod.record_search(self.conn, self.entry.get().strip())
        try:
            url, exact = market_mod.url_for(self.conn, name)
        except Exception as exc:
            print(f"could not open the market: {exc}")
            return "break"

        if not exact and not self._has_flea_price():
            # No page and no price means the item is not tradeable at all -
            # a quest item, an ammo box, a piece of intel. Opening a market
            # search for it would just show an empty table, so say why
            # instead of hiding the window behind a dead end.
            self._set_detail([
                (f"{name}\n", "head"),
                ("  no market page\n\n", "warn"),
                ("This item is not traded on the flea market - quest items,\n"
                 "ammo boxes and intel have no listing and no price.\n\n", "dim"),
                ("Ctrl+Enter still opens it on the wiki.\n", "label"),
            ])
            return "break"

        self.hide()
        self.root.update_idletasks()
        try:
            extracts_mod.open_in_browser(url, reuse_title="TarkovForge")
        except Exception as exc:
            print(f"could not open the market: {exc}")
        return "break"

    def _has_flea_price(self) -> bool:
        """Whether the highlighted item is traded at all."""
        item_id = self._selected_item_id()
        if not item_id:
            return False
        try:
            return bool(self.conn.execute(
                "SELECT 1 FROM flea_prices WHERE item_id = ?", (item_id,)
            ).fetchone())
        except Exception:
            return False

    def _open_wiki(self, event=None):
        """Open the highlighted row's wiki article.

        Hidden first for the same reason as the map: reusing a tab types the
        URL with Ctrl+L and those keystrokes must not land in the search box.
        """
        from . import extracts as extracts_mod
        from . import wiki as wikimod

        name = wikimod.clean_name(self._wiki_name() or "")
        if not name:
            return "break"
        searchmod.record_search(self.conn, self.entry.get().strip())
        self.hide()
        self.root.update_idletasks()
        try:
            url, title = wikimod.page_for(name)
            extracts_mod.open_in_browser(url, reuse_title=title)
        except Exception as exc:
            print(f"could not open the wiki: {exc}")
        return "break"

    def _nav_enter(self, event=None):
        if self.qty_target:
            return self._commit_quantity()
        if self.view == "slots":
            self._enter_parts(self._current_row())
            return "break"
        if self.view == "parts":
            self._enter_fits()  # a part opens the weapons it goes on
            return "break"
        if self.view == "fits":
            gun = self._current_subject()
            if gun:
                self._enter_slots(gun)  # and a weapon opens its categories
            return "break"
        chosen = self._current_result()
        if chosen:
            if chosen.get("kind") == "recent":
                self.entry.delete(0, "end")
                self.entry.insert(0, chosen["name"])
                self._set_mode("search")
                self._on_type()
                return "break"
            searchmod.record_search(self.conn, self.entry.get().strip())
            if chosen.get("kind") == "extract":
                self._open_extract(chosen["id"])
            elif chosen.get("kind") == "weapon":
                self._enter_slots(chosen)
            else:
                self._enter_fits()
        return "break"

    def _on_select(self, event=None) -> None:
        current = self.listbox.curselection()
        if not current:
            return
        if self.view == "slots":
            if current[0] < len(self.slot_entries):
                self._render_slot(current[0])
            return
        if self.view != "search":
            subject = self._current_subject()
            if subject:
                self._render(subject["id"])
            return
        entry = self._current_result()
        if entry:
            self._render(entry["id"])

    # --- rendering -----------------------------------------------------

    def _set_detail(self, chunks) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        for text, tag in chunks:
            self.detail.insert("end", text, tag)
        self.detail.configure(state="disabled")

    def _append_detail(self, chunks) -> None:
        """Add to what the detail pane already shows, rather than replacing it."""
        if not chunks:
            return
        self.detail.configure(state="normal")
        for text, tag in chunks:
            self.detail.insert("end", text, tag)
        self.detail.configure(state="disabled")

    def _render(self, item_id: str) -> None:
        if isinstance(item_id, str) and item_id.startswith("recent:"):
            term = item_id.split(":", 1)[1]
            self._set_detail([
                (f"{term}\n\n", "head"),
                ("  a search you ran before\n\n", "dim"),
                ("  Enter searches for it again\n", "label"),
                ("  Ctrl+Del forgets it\n", "dim"),
            ])
            return
        data = searchmod.describe(self.conn, item_id)
        if not data:
            self._set_detail([("not found\n", "dim")])
            return
        self._set_detail(self._format(data))

    def _format_needs(self, needs: list[dict]) -> list[tuple[str, str]]:
        """What still wants this item, tasks before hideout, unlocked first."""
        if not needs:
            return []
        out: list[tuple[str, str]] = [("  STILL NEEDED\n", "head")]
        for entry in needs[:10]:
            outstanding = (entry.get("need") or 0) - (entry.get("have") or 0)
            fir = " FIR" if entry.get("found_in_raid") else "    "
            # A wide "any of N" objective means this item is one option among
            # many, not something specifically required.
            alternatives = entry.get("alternatives") or 1
            alt = f"  (1 of {alternatives} accepted)" if alternatives > 1 else ""
            locked = "" if entry.get("available") else "  [locked]"
            tag = "good" if entry.get("available") else "dim"
            out.append((f"    {outstanding:>3}x", "warn"))
            out.append((f"{fir}  ", "warn" if entry.get("found_in_raid") else "dim"))
            out.append((f"{entry.get('source_name')}{alt}{locked}\n", tag))
        if len(needs) > 10:
            out.append((f"    ... and {len(needs) - 10} more\n", "dim"))
        out.append(("\n", None))
        return out

    def _format_extract(self, extract: dict) -> list[tuple[str, str]]:
        side = (extract.get("side") or "").replace("Pmc", "PMC").replace("Coop", "Co-op")
        out: list[tuple[str, str]] = [
            (f"{extract.get('display_name')}\n", "head"),
            (f"  {extract.get('map_name')}\n\n", "dim"),
        ]
        out.append(("  side        ", "label"))
        out.append((f"{side or '-'}\n", None))
        if extract.get("chance") is not None:
            out.append(("  chance      ", "label"))
            colour = "good" if (extract["chance"] or 0) >= 100 else "warn"
            out.append((f"{extract['chance']:.0f}%\n", colour))
        if extract.get("exfil_time"):
            out.append(("  exfil time  ", "label"))
            out.append((f"{extract['exfil_time']}s\n", None))
        # requirement_text turns the raw code and its locale tip into prose,
        # dropping unsubstituted templates and developer notes.
        from . import extracts as extracts_mod

        requirement = extracts_mod.requirement_text(extract)
        if requirement:
            out.append(("  requires    ", "label"))
            out.append((f"{requirement}\n", "warn"))
        if extract.get("entry_points"):
            out.append(("  spawns      ", "label"))
            out.append((f"{extract['entry_points']}\n", None))

        out.append(("\n  Press Enter to open the interactive map here.\n", "head"))
        if not extract.get("marker_id"):
            out.append(("  (no map marker for this one - opens the map unfocused)\n", "dim"))
        return out

    def _stash_footer(self) -> list[tuple[str, str]]:
        """Totals and the editing keys, shown under the Have list."""
        rows = searchmod.stash_contents(self.conn, "have")
        if not rows:
            return []
        total = sum(r["line_value"] or 0 for r in rows)
        units = sum(r["quantity"] or 0 for r in rows)
        return [
            ("\n  ─────────────\n", "dim"),
            (f"  {len(rows)} kinds, {units} items, ", "dim"),
            (f"{_fmt_price(total)} RUB", "good"),
            (" at flea prices\n", "dim"),
            ("  Ctrl+Shift+H change count    Ctrl+Del remove\n", "label"),
        ]

    def _stash_line(self, item_id: str) -> list[tuple[str, str]]:
        """How many of this you have, and how to change it.

        The keys are spelled out on the item rather than left to the hint bar:
        holding a count is the point at which you want to edit or drop it, and
        that is exactly when you are looking at this pane.
        """
        out: list[tuple[str, str]] = []
        if item_id in self.have:
            count = self.have_qty.get(item_id, 1)
            out.append((f"  {MARK_HAVE} in your stash", "good"))
            out.append((f" x{count}\n" if count > 1 else "\n", "good"))
            out.append(("    Ctrl+Shift+H change count   "
                        "Ctrl+Up/Down +1/-1   Ctrl+Del remove\n", "dim"))
        if item_id in self.watch:
            out.append((f"  {MARK_WATCH} watching for it", "good"))
            out.append(("    Ctrl+D to stop\n", "dim"))
        return out

    def _remove_from_stash(self, event=None) -> str:
        """Drop the highlighted item off the have list outright."""
        entry = self._current_result() or {}
        if entry.get("kind") == "recent":
            searchmod.forget_search(self.conn, entry["name"])
            self.status_note = "forgotten"
            self._on_type()
            return "break"
        item_id = self._selected_item_id()
        if not item_id or item_id not in self.have:
            return "break"
        name = (
            (self._current_subject() or self._current_result() or {}).get("name") or ""
        ).replace("[DEMO] ", "")
        searchmod.set_quantity(self.conn, item_id, "have", 0)
        self._refresh_marks()
        self._rebuild_filter_bar()
        self.status_note = f"removed {name}" if name else "removed from your stash"
        self._redraw_current()
        return "break"

    def _flea_line(self, data: dict) -> list[tuple[str, str]]:
        """Price, value per slot, and which way it is moving.

        Value per slot is the number that decides what comes home: a 200k
        item filling six slots loses to a 90k item filling one. It is left
        off weapons, whose footprint depends on what is bolted to them.
        """
        flea = data.get("flea")
        if not flea:
            price = (data.get("item") or {}).get("avg_24h_price")
            if price:
                return [(f"  flea {_fmt_price(price)} RUB\n", "dim")]
            return [("  not on the flea market\n", "dim")]

        out = [("  flea ", "label"), (f"{_fmt_price(flea['price'])} RUB", "good")]
        if flea.get("per_slot"):
            slots = flea.get("slots") or 1
            out.append((f"   {_fmt_price(flea['per_slot'])}/slot", None))
            if slots > 1:
                out.append((f" ({slots} slots)", "dim"))
        change = flea.get("change_pct") or 0
        if abs(change) >= 0.5:
            out.append((f"   {change:+.0f}%", "good" if change > 0 else "warn"))
        out.append(("\n", None))
        return out

    def _format_part(self, data: dict) -> list[tuple[str, str]]:
        item, mod = data["item"], data.get("mod") or {}
        fits = data.get("fits") or []
        slots = (fits[0].get("_slots") if fits else None) or []
        out: list[tuple[str, str]] = [(f"{item['name']}\n", "head")]
        out += self._stash_line(item["id"])
        out += self._flea_line(data)
        out.append((f"  {' / '.join(slots) or 'attachment'}\n\n", "dim"))

        for label, key, better_when_negative in (
            ("ergonomics", "ergonomics", False), ("recoil", "recoil_modifier", True),
            ("accuracy", "accuracy_modifier", False), ("loudness", "loudness", True),
        ):
            value = mod.get(key)
            if value is None:
                continue
            good = (value < 0) if better_when_negative else (value > 0)
            out.append((f"  {label:<11}", "label"))
            out.append((f"{_signed(value)}\n", "good" if good and value else "dim"))
        if mod.get("weight"):
            out.append(("  weight     ", "label"))
            out.append((f"{mod['weight']} kg\n", "dim"))

        out += self._format_needs(data.get("needs") or [])

        if fits:
            out.append((f"\n  FITS ON  ({len(fits)} weapons)\n", "head"))
            for gun in fits[:14]:
                out.append((f"    {(gun['name'] or '')[:46]:<46}", None))
                out.append((f"  {gun.get('caliber') or ''}\n", "dim"))
            if len(fits) > 14:
                out.append((f"    ... and {len(fits) - 14} more\n", "dim"))
            out.append(("\n  Enter walks all of them as rows.\n", "dim"))
        else:
            out.append(("\n  no weapon takes this directly\n", "dim"))
        return out

    def _format(self, data: dict) -> list[tuple[str, str]]:
        if data.get("kind") == "extract":
            return self._format_extract(data.get("extract") or {})
        if data.get("kind") == "part":
            return self._format_part(data)

        out: list[tuple[str, str]] = []
        item = data["item"]
        stats = data.get("stats") or {}
        kind = data["kind"]

        out.append((f"{item['name']}\n", "head"))
        out += self._stash_line(item["id"])
        out += self._flea_line(data)
        out.append(("\n", None))
        # Quest and hideout demand goes above the stats: when an item is in
        # your hands, "do I still need this?" is the first question.
        out += self._format_needs(data.get("needs") or [])

        if kind == "ammo":
            out.append(("  pen ", "label"))
            out.append((f"{stats.get('penetration_power')}", f"pen{self._band(stats.get('penetration_power'))}"))
            out.append(("   dmg ", "label"))
            out.append((f"{stats.get('damage')}", None))
            out.append(("   armor dmg ", "label"))
            out.append((f"{stats.get('armor_damage')}%", None))
            frag = stats.get("fragmentation_chance")
            out.append(("   frag ", "label"))
            out.append((f"{frag * 100:.0f}%\n" if isinstance(frag, float) else "-\n", None))
            out.append((f"  {stats.get('caliber') or ''}   {stats.get('initial_speed') or '-'} m/s\n\n", "dim"))
            out += self._table("FIRED BY", data.get("weapons"), self._gun_row)
            out += self._table("FITS MAGAZINES", data.get("magazines"), self._mag_row, MAG_HEADER)

        elif kind == "weapon":
            out.append((f"  {stats.get('caliber') or ''}\n", "dim"))
            out.append(("  ergo ", "label"))
            out.append((f"{stats.get('ergonomics')}", None))
            out.append(("   recoil v/h ", "label"))
            out.append((f"{stats.get('recoil_vertical')}/{stats.get('recoil_horizontal')}", None))
            out.append(("   rpm ", "label"))
            out.append((f"{stats.get('fire_rate')}\n\n", None))
            out += self._table("AMMO (best penetration first)", data.get("ammo"), self._ammo_row)
            out += self._table("MAGAZINES", data.get("magazines"), self._mag_row, MAG_HEADER)

        elif kind == "magazine":
            out.append(("  capacity ", "label"))
            out.append((f"{stats.get('capacity')}", None))
            out.append(("   ergo ", "label"))
            out.append((f"{stats.get('ergonomics')}\n\n", None))
            out += self._table("ACCEPTS AMMO", data.get("ammo"), self._ammo_row)
            out += self._table("FITS WEAPONS", data.get("weapons"), self._gun_row)

        offers = data.get("offers") or []
        if offers:
            out.append(("\n  TRADERS\n", "head"))
            for o in offers:
                lvl = f"LL{o['min_level']}" if o.get("min_level") else "  "
                out.append((f"    {o['vendor']:<14} {lvl:<5} {_fmt_price(o.get('price'))} {o.get('currency') or ''}\n", None))
        return out

    @staticmethod
    def _band(pen) -> int:
        if not isinstance(pen, (int, float)):
            return 999
        for threshold, _ in PEN_BANDS:
            if pen < threshold:
                return threshold
        return 999

    def _table(self, title, rows, row_fn, header: str | None = None) -> list[tuple[str, str]]:
        if not rows:
            return []
        out = [(f"  {title}\n", "head")]
        if header:
            out.append((f"{header}\n", "dim"))
        for r in rows:
            out += row_fn(r)
        out.append(("\n", None))
        return out

    def _ammo_row(self, r) -> list[tuple[str, str]]:
        name = (r["name"] or "").replace("[DEMO] ", "")
        pen = r.get("penetration_power")
        return [
            ("    pen ", "dim"),
            (f"{pen:>3}", f"pen{self._band(pen)}"),
            (f"  dmg {r.get('damage'):>3}  {name[:44]:<44}{_price_col(r)}\n", None),
        ]

    def _mag_row(self, r) -> list[tuple[str, str]]:
        name = (r["name"] or "").replace("[DEMO] ", "")
        return [(f"    {r.get('capacity'):>4} {str(r.get('ergonomics')):>6}  "
                 f"{name[:58]:<58}{_price_col(r)}\n", None)]

    def _gun_row(self, r) -> list[tuple[str, str]]:
        name = (r["name"] or "").replace("[DEMO] ", "")
        return [(f"    {name[:50]:<50} ergo {str(r.get('ergonomics')):>5}  "
                 f"rec {r.get('recoil_vertical')}\n", None)]

    def run(self) -> None:
        self.root.mainloop()


def main(hotkey: str | None = None) -> int:
    cfg = load_config()["search"]
    spec = hotkey or cfg["hotkey"]
    conn = dbmod.connect()

    total = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    if total == 0:
        print("The database is empty. Run:  python -m tarkov_tools.cli sync")
        return 1

    popover = Popover(conn, cfg["max_results"])

    from .hotkey import HotkeyError, HotkeyListener

    listener = None
    try:
        listener = HotkeyListener(spec, popover.request_toggle)
        listener.start()
        print(f"listening for {spec} - press it to open the search popover")
    except HotkeyError as exc:
        print(f"warning: {exc}\nShowing the window directly instead.")
        popover.show()

    print("Close this window or press Ctrl-C to stop.")
    try:
        popover.run()
    finally:
        if listener:
            listener.stop()
        conn.close()
    return 0
