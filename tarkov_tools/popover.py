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

BG = "#14161a"
BG_ALT = "#1b1e24"
FG = "#d7dae0"
FG_DIM = "#7c8391"
ACCENT = "#c8a45c"
SEL = "#2a3038"
TITLE_BG = "#0f1114"

# Column labels for magazine tables, so each row need not repeat them.
MAG_HEADER = "     cap   ergo  name"

# Tab cycles these. (label, kind, side) - kind/side of None means no narrowing.
# With the search box empty, a filter lists that whole category.
BASE_FILTERS = (
    ("All", None, None),
    ("Guns", "weapon", None),
    ("Ammo", "ammo", None),
    ("Mags", "magazine", None),
)
# Only offered once a TarkovTracker account has been synced. The integration
# is optional, and an empty filter is worse than no filter.
TRACKER_FILTER = ("Needed", "needed", None)
EXTRACT_FILTERS = (
    ("Extracts", "extract", None),
    ("Exfil PMC", "extract", "Pmc"),
    ("Exfil Scav", "extract", "Scav"),
    ("Exfil Co-op", "extract", "Coop"),
)


def build_filters(conn) -> tuple:
    """The filter set this database can actually serve."""
    filters = list(BASE_FILTERS)
    if searchmod.tracker_configured(conn):
        filters.append(TRACKER_FILTER)
    try:
        has_extracts = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='extracts'"
        ).fetchone() and conn.execute("SELECT 1 FROM extracts LIMIT 1").fetchone()
    except Exception:
        has_extracts = False
    if has_extracts:
        filters.extend(EXTRACT_FILTERS)
    return tuple(filters)

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
        self._size = (1000, 620)
        self.filter_index = 0
        self.filters = build_filters(conn)
        self._build()

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
        self.entry.bind("<KeyRelease>", self._on_type)
        self.entry.bind("<Down>", self._nav_down)
        self.entry.bind("<Up>", self._nav_up)
        self.entry.bind("<Return>", self._nav_enter)
        self.entry.bind("<Escape>", lambda e: self.hide())
        # Tab would otherwise move focus out of the entry, so both bindings
        # return "break". (ISO_Left_Tab is an X11 keysym and is not valid on
        # Windows Tk - Shift-Tab is the portable spelling.)
        self.entry.bind("<Tab>", self._next_filter)
        self.entry.bind("<Shift-Tab>", self._prev_filter)

        # filter chips
        self.filter_bar = tk.Frame(inner, bg=BG_ALT)
        self.filter_bar.pack(fill="x")
        self.filter_labels = []
        tk.Label(self.filter_bar, text="  Tab ", bg=BG_ALT, fg="#5a606c",
                 font=(mono, 9)).pack(side="left")
        for index, (label, _kind, _side) in enumerate(self.filters):
            widget = tk.Label(self.filter_bar, text=f" {label} ", bg=BG_ALT,
                              fg=FG_DIM, font=(mono, 9), padx=4)
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
        self.listbox.bind("<Escape>", lambda e: self.hide())

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

        hint = tk.Label(
            inner,
            text="  type to search   Tab filter   ↑↓ move   Enter details / open map   Esc hide  ",
            bg=BG_ALT, fg=FG_DIM, font=(mono, 9), anchor="w",
        )
        hint.pack(fill="x", side="bottom")
        self._make_draggable(hint)

        self.root.bind("<Escape>", lambda e: self.hide())
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self._set_filter(0)
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

    def _pump(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event == "toggle":
                    self.toggle()
        except queue.Empty:
            pass
        self.root.after(60, self._pump)

    # --- filters -------------------------------------------------------

    def _set_filter(self, index: int) -> str:
        self.filter_index = index % len(self.filters)
        for position, widget in enumerate(self.filter_labels):
            active = position == self.filter_index
            widget.configure(
                fg=BG if active else FG_DIM,
                bg=ACCENT if active else BG_ALT,
            )
        self._on_type()
        return "break"

    def _next_filter(self, event=None) -> str:
        return self._set_filter(self.filter_index + 1)

    def _prev_filter(self, event=None) -> str:
        return self._set_filter(self.filter_index - 1)

    # --- search --------------------------------------------------------

    def _on_type(self, event=None) -> None:
        if event is not None and event.keysym in ("Up", "Down", "Return", "Escape",
                                                  "Tab", "ISO_Left_Tab"):
            return
        term = self.entry.get().strip()
        _label, kind, side = self.filters[self.filter_index]
        # An active filter with an empty box lists that whole category, so the
        # filter doubles as a way to browse.
        if term or kind:
            self.results = searchmod.search(
                self.conn, term, self.max_results, kind=kind, side=side
            )
        else:
            self.results = []
        self.listbox.delete(0, "end")
        for r in self.results:
            tag = {"weapon": "GUN", "ammo": "AMO", "magazine": "MAG",
                   "extract": "EXT", "needed": "NEED"}.get(r["kind"], "   ")
            name = (r["name"] or "").replace("[DEMO] ", "")
            if r["kind"] == "needed" and r.get("short_name"):
                name = f"{name} · {r['short_name']}"
            elif r["kind"] == "extract" and r.get("short_name"):
                # Two extracts can share a name (a PMC and a Scav one), so the
                # map is shown here and the side in the detail pane.
                name = f"{name} · {r['short_name']}"
            self.listbox.insert("end", f"{tag}  {name[:34]}")
        if self.results:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(0)
            self._render(self.results[0]["id"])
        else:
            self._set_detail([("no matches\n", "dim")] if term else
                             [("type a gun, round or magazine name\n", "dim")])

    def _move(self, delta: int) -> None:
        if not self.results:
            return
        current = self.listbox.curselection()
        index = (current[0] if current else 0) + delta
        index = max(0, min(len(self.results) - 1, index))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.see(index)
        self._render(self.results[index]["id"])

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
        reuse = f"Map:{extract.get('wiki_page')}" if extract.get("wiki_page") else None
        self.hide()
        self.root.update_idletasks()
        try:
            opened, _ = extracts_mod.open_in_browser(url, reuse_title=reuse)
            cfg = load_config().get("extracts", {})
            if opened and cfg.get("apply_map_filters", True) and extract.get("wiki_page"):
                extracts_mod.apply_map_filters(
                    extract["wiki_page"], cfg.get("categories")
                )
        except Exception as exc:
            print(f"could not open the map: {exc}")

    def _nav_enter(self, event=None):
        current = self.listbox.curselection()
        if self.results:
            chosen = self.results[current[0] if current else 0]
            if chosen.get("kind") == "extract":
                self._open_extract(chosen["id"])
            else:
                self._render(chosen["id"])
        return "break"

    def _on_select(self, event=None) -> None:
        current = self.listbox.curselection()
        if current and self.results:
            self._render(self.results[current[0]]["id"])

    # --- rendering -----------------------------------------------------

    def _set_detail(self, chunks) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        for text, tag in chunks:
            self.detail.insert("end", text, tag)
        self.detail.configure(state="disabled")

    def _render(self, item_id: str) -> None:
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
        requirement = extract.get("requirement")
        if requirement and requirement != "None":
            tip = extract.get("requirement_tip")
            out.append(("  requires    ", "label"))
            out.append((f"{requirement}{f'  ({tip})' if tip else ''}\n", "warn"))
        if extract.get("entry_points"):
            out.append(("  spawns      ", "label"))
            out.append((f"{extract['entry_points']}\n", None))

        out.append(("\n  Press Enter to open the interactive map here.\n", "head"))
        if not extract.get("marker_id"):
            out.append(("  (no map marker for this one - opens the map unfocused)\n", "dim"))
        return out

    def _format(self, data: dict) -> list[tuple[str, str]]:
        if data.get("kind") == "extract":
            return self._format_extract(data.get("extract") or {})

        out: list[tuple[str, str]] = []
        item = data["item"]
        stats = data.get("stats") or {}
        kind = data["kind"]

        out.append((f"{item['name']}\n", "head"))
        price = item.get("avg_24h_price")
        out.append((f"  flea avg {_fmt_price(price)} RUB\n\n", "dim"))
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
