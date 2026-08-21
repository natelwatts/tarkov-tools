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

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

BG = "#14161a"
BG_ALT = "#1b1e24"
FG = "#d7dae0"
FG_DIM = "#7c8391"
ACCENT = "#c8a45c"
SEL = "#2a3038"

# Penetration bands used for the colour cue, matching how players talk about
# armour class: roughly "handles class N".
PEN_BANDS = [(20, "#8a5a5a"), (30, "#8a7a4a"), (37, "#7a8a4a"), (45, "#5a8a5a"), (999, "#4a8a7a")]


def _force_foreground(hwnd: int) -> None:
    """Bring a window to the front even when another app owns the foreground.

    Windows blocks a background process from stealing focus unless its input
    thread is attached to the current foreground thread, hence the dance.
    """
    try:
        fg = user32.GetForegroundWindow()
        if not fg or fg == hwnd:
            user32.SetForegroundWindow(hwnd)
            return
        target_tid = user32.GetWindowThreadProcessId(fg, None)
        our_tid = kernel32.GetCurrentThreadId()
        attached = user32.AttachThreadInput(target_tid, our_tid, True)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        if attached:
            user32.AttachThreadInput(target_tid, our_tid, False)
    except Exception:
        pass


def _fmt_price(value) -> str:
    return f"{value:,}" if isinstance(value, int) and value else "-"


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
        self._build()

    # --- construction --------------------------------------------------

    def _build(self) -> None:
        self.root = tk.Tk()
        self.root.title("Tarkov Tools")
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        width, height = 1000, 620
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{width}x{height}+{(sw - width) // 2}+{(sh - height) // 3}")

        mono = "Consolas" if "Consolas" in tkfont.families() else "Courier New"
        self.font_entry = (mono, 16)
        self.font_list = (mono, 11)
        self.font_detail = (mono, 11)

        outer = tk.Frame(self.root, bg=ACCENT, padx=1, pady=1)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=BG)
        inner.pack(fill="both", expand=True)

        # search row
        row = tk.Frame(inner, bg=BG_ALT)
        row.pack(fill="x", padx=0, pady=0)
        tk.Label(row, text="  search ", bg=BG_ALT, fg=ACCENT, font=self.font_entry).pack(side="left")
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

        body = tk.Frame(inner, bg=BG)
        body.pack(fill="both", expand=True)

        self.listbox = tk.Listbox(
            body, bg=BG, fg=FG, font=self.font_list, relief="flat",
            highlightthickness=0, selectbackground=SEL, selectforeground=ACCENT,
            activestyle="none", width=38,
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
            text="  type to search   ↑↓ move   Enter details   Esc hide  ",
            bg=BG_ALT, fg=FG_DIM, font=(mono, 9), anchor="w",
        )
        hint.pack(fill="x", side="bottom")

        self.root.bind("<Escape>", lambda e: self.hide())
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.withdraw()
        self.root.after(60, self._pump)

    # --- visibility ----------------------------------------------------

    def show(self) -> None:
        self._visible = True
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        _force_foreground(self.root.winfo_id())
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

    # --- search --------------------------------------------------------

    def _on_type(self, event=None) -> None:
        if event is not None and event.keysym in ("Up", "Down", "Return", "Escape"):
            return
        term = self.entry.get().strip()
        self.results = searchmod.search(self.conn, term, self.max_results) if term else []
        self.listbox.delete(0, "end")
        for r in self.results:
            tag = {"weapon": "GUN", "ammo": "AMO", "magazine": "MAG"}.get(r["kind"], "   ")
            name = (r["name"] or "").replace("[DEMO] ", "")
            self.listbox.insert("end", f"{tag}  {name[:32]}")
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

    def _nav_enter(self, event=None):
        current = self.listbox.curselection()
        if self.results:
            self._render(self.results[current[0] if current else 0]["id"])
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

    def _format(self, data: dict) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        item = data["item"]
        stats = data.get("stats") or {}
        kind = data["kind"]

        out.append((f"{item['name']}\n", "head"))
        price = item.get("avg_24h_price")
        out.append((f"  flea avg {_fmt_price(price)} RUB\n\n", "dim"))

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
            out += self._table("FITS MAGAZINES", data.get("magazines"), self._mag_row)

        elif kind == "weapon":
            out.append((f"  {stats.get('caliber') or ''}\n", "dim"))
            out.append(("  ergo ", "label"))
            out.append((f"{stats.get('ergonomics')}", None))
            out.append(("   recoil v/h ", "label"))
            out.append((f"{stats.get('recoil_vertical')}/{stats.get('recoil_horizontal')}", None))
            out.append(("   rpm ", "label"))
            out.append((f"{stats.get('fire_rate')}\n\n", None))
            out += self._table("AMMO (best penetration first)", data.get("ammo"), self._ammo_row)
            out += self._table("MAGAZINES", data.get("magazines"), self._mag_row)

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

    def _table(self, title, rows, row_fn) -> list[tuple[str, str]]:
        if not rows:
            return []
        out = [(f"  {title}\n", "head")]
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
            (f"  dmg {r.get('damage'):>3}  {name[:34]:<34} {_fmt_price(r.get('avg_24h_price')):>9}\n", None),
        ]

    def _mag_row(self, r) -> list[tuple[str, str]]:
        name = (r["name"] or "").replace("[DEMO] ", "")
        return [(f"    {r.get('capacity'):>3} rnd  ergo {str(r.get('ergonomics')):>6}  "
                 f"{name[:32]:<32} {_fmt_price(r.get('avg_24h_price')):>9}\n", None)]

    def _gun_row(self, r) -> list[tuple[str, str]]:
        name = (r["name"] or "").replace("[DEMO] ", "")
        return [(f"    {name[:36]:<36} ergo {str(r.get('ergonomics')):>5}  "
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
