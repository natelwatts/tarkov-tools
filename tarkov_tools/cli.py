"""Command line entry point for tarkov-tools.

  uv run tarkov-tools                  gamma watcher + search popover together
  uv run tarkov-tools start 1.6        same, with an explicit gamma value

  uv run tarkov-tools gamma watch      gamma only, while Tarkov has focus
  uv run tarkov-tools gamma set 1.5    set it now
  uv run tarkov-tools gamma reset      back to 1.0
  uv run tarkov-tools gamma displays   list monitors and current gamma

  uv run tarkov-tools import-templates --download   build the db from game files
  uv run tarkov-tools sync             build/refresh from the tarkov.dev API
  uv run tarkov-tools search m995      look something up in the terminal
  uv run tarkov-tools ammo             penetration chart by caliber
  uv run tarkov-tools popover          search popover only
  uv run tarkov-tools extract zb-1011  open the wiki map, extract highlighted
  uv run tarkov-tools hotkey ctrl+t    rebind the popover hotkey

'tt' is a shorter alias for 'tarkov-tools'.
"""

from __future__ import annotations

import argparse
import sys

from .config import load_config, write_default_config


# --- gamma -------------------------------------------------------------

def _cmd_gamma(args: argparse.Namespace) -> int:
    from . import gamma as gm

    if args.unlock_range:
        if gm.gamma_range_unlocked():
            print("GdiIcmGammaRange is already 256 - no clamping.")
            return 0
        if gm.unlock_gamma_range():
            print("Set GdiIcmGammaRange=256. Sign out and back in for it to take effect.")
            return 0
        print(
            "Could not write the registry value. Re-run from an Administrator shell:\n"
            "  python -m tarkov_tools.cli gamma --unlock-range",
            file=sys.stderr,
        )
        return 1

    cfg = load_config()["gamma"]

    if args.action == "displays":
        from .winapi import display_for_window, foreground_window, get_gamma_ramp, list_displays

        here = display_for_window(foreground_window())
        for name, desc in list_displays():
            try:
                mid = get_gamma_ramp(name)[0][128]
                state = f"midtone={mid} ({'neutral' if mid == 32896 else 'modified'})"
            except OSError as exc:
                state = f"gamma unavailable ({exc})"
            marker = "  <- this window" if name == here else ""
            print(f"{name}  {desc}  {state}{marker}")
        return 0

    value = args.value if args.value is not None else cfg["value"]

    if args.action == "set":
        ctrl = gm.GammaController(value, cfg["brightness"], cfg["contrast"])
        results = ctrl.apply()
        ctrl._applied_on.clear()  # deliberate: leave it applied after we exit
        for name, ok in results.items():
            print(f"{name}: {'gamma ' + str(value) if ok else 'REJECTED'}")
        return 0 if all(results.values()) else 1

    if args.action == "reset":
        from .winapi import list_displays, set_gamma_ramp

        neutral = gm.build_ramp(1.0)
        for name, _ in list_displays():
            print(f"{name}: {'reset to 1.0' if set_gamma_ramp(name, neutral) else 'FAILED'}")
        return 0

    gm.watch(
        gamma=value,
        brightness=cfg["brightness"],
        contrast=cfg["contrast"],
        exes=tuple(cfg["exes"]),
        poll_seconds=cfg["poll_seconds"],
        game_monitor_only=cfg["game_monitor_only"] and not args.all_displays,
        companion_titles=tuple(cfg.get("companion_titles") or ()),
        companion_classes=tuple(cfg.get("companion_classes") or ()),
        revert_grace_seconds=cfg.get("revert_grace_seconds", 0.6),
    )
    return 0


# --- data --------------------------------------------------------------

def _cmd_sync(args: argparse.Namespace) -> int:
    from .ingest import sync

    counts = sync(use_cache=args.cache)
    print("\ndatabase now holds:")
    for table, count in counts.items():
        print(f"  {table:18} {count}")
    return 0


def _cmd_import_templates(args: argparse.Namespace) -> int:
    from .templates import run_import

    counts = run_import(
        items_path=args.items,
        locale_path=args.locale,
        do_download=args.download,
    )
    print("\ndatabase now holds:")
    for table, count in counts.items():
        print(f"  {table:18} {count}")
    print("\nNote: templates carry no market prices - price columns are left as-is.")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    from . import db as dbmod
    from . import search as searchmod

    conn = dbmod.connect()
    term = " ".join(args.term)
    results = searchmod.search(conn, term, load_config()["search"]["max_results"])
    if not results:
        print(f"no matches for {term!r}")
        return 1

    if args.list:
        for r in results:
            print(f"[{r['kind']:8}] {r['name']}")
        return 0

    data = searchmod.describe(conn, results[0]["id"])
    _print_detail(data)
    if len(results) > 1:
        print("\nalso matched: " + ", ".join(r["name"] for r in results[1:6]))
    conn.close()
    return 0


def _print_detail(data: dict) -> None:
    if data.get("kind") == "extract":
        e = data.get("extract") or {}
        side = (e.get("side") or "").replace("Pmc", "PMC").replace("Coop", "Co-op")
        print(f"\n{e.get('display_name')}   [{e.get('map_name')}]   (extract)")
        print(f"  side          {side or '-'}")
        if e.get("chance") is not None:
            print(f"  chance        {e['chance']:.0f}%")
        if e.get("exfil_time"):
            print(f"  exfil time    {e['exfil_time']}s")
        from . import extracts as ex_mod

        requirement = ex_mod.requirement_text(e)
        if requirement:
            print(f"  requirement   {requirement}")
        if e.get("entry_points"):
            print(f"  spawns        {e['entry_points']}")
        print("\n  open the map with:  uv run tarkov-tools extract "
              f"\"{e.get('display_name')}\"")
        return

    item, stats, kind = data["item"], data.get("stats") or {}, data["kind"]
    money = lambda v: f"{v:,}" if isinstance(v, int) and v else "-"  # noqa: E731

    print(f"\n{item['name']}   [{kind}]")
    print(f"  flea avg {money(item.get('avg_24h_price'))} RUB")

    if kind == "ammo":
        frag = stats.get("fragmentation_chance")
        frag_text = f"{frag * 100:.0f}%" if isinstance(frag, float) else "-"
        print(f"  pen {stats.get('penetration_power')}   dmg {stats.get('damage')}   "
              f"armor dmg {stats.get('armor_damage')}%   frag {frag_text}")
        print(f"  {stats.get('caliber')}   {stats.get('initial_speed')} m/s")
        _print_rows("FIRED BY", data.get("weapons"),
                    lambda r: f"    {r['name']}")
        _print_rows("FITS MAGAZINES", data.get("magazines"),
                    lambda r: f"    {r['capacity']:>3} rnd  {r['name']}")
    elif kind == "weapon":
        print(f"  {stats.get('caliber')}   ergo {stats.get('ergonomics')}   "
              f"recoil {stats.get('recoil_vertical')}/{stats.get('recoil_horizontal')}   "
              f"rpm {stats.get('fire_rate')}")
        _print_rows("AMMO (best penetration first)", data.get("ammo"),
                    lambda r: f"    pen {r['penetration_power']:>3}  dmg {r['damage']:>3}  "
                              f"{r['name']:<38} {money(r.get('avg_24h_price')):>10}")
        _print_rows("MAGAZINES", data.get("magazines"),
                    lambda r: f"    {r['capacity']:>3} rnd  ergo {str(r['ergonomics']):>6}  {r['name']}")
    elif kind == "magazine":
        print(f"  capacity {stats.get('capacity')}   ergo {stats.get('ergonomics')}")
        _print_rows("ACCEPTS AMMO", data.get("ammo"),
                    lambda r: f"    pen {r['penetration_power']:>3}  dmg {r['damage']:>3}  {r['name']}")
        _print_rows("FITS WEAPONS", data.get("weapons"),
                    lambda r: f"    {r['name']}")

    offers = data.get("offers") or []
    if offers:
        print("\n  TRADERS")
        for o in offers:
            level = f"LL{o['min_level']}" if o.get("min_level") else "   "
            print(f"    {o['vendor']:<14} {level:<5} {money(o.get('price'))} {o.get('currency') or ''}")


def _print_rows(title: str, rows, fmt) -> None:
    if not rows:
        return
    print(f"\n  {title}")
    for row in rows:
        print(fmt(row))


def _cmd_ammo(args: argparse.Namespace) -> int:
    from . import db as dbmod
    from . import search as searchmod

    conn = dbmod.connect()
    if args.list_calibers:
        for c in searchmod.calibers(conn):
            print(c)
        return 0

    rows = searchmod.ammo_chart(conn, args.caliber)
    if not rows:
        print("no ammo found - run 'sync' first, or check the caliber name")
        return 1

    current = None
    for r in rows:
        if r["caliber"] != current:
            current = r["caliber"]
            print(f"\n{current}")
            print(f"  {'pen':>4} {'dmg':>4} {'armor':>6} {'frag':>5}  name")
        frag = r.get("fragmentation_chance")
        frag_text = f"{frag * 100:>4.0f}%" if isinstance(frag, float) else "    -"
        print(f"  {r['penetration_power']:>4} {r['damage']:>4} "
              f"{str(r['armor_damage']) + '%':>6} {frag_text}  {r['name']}")
    conn.close()
    return 0


def _cmd_popover(args: argparse.Namespace) -> int:
    from .popover import main as popover_main

    return popover_main(args.hotkey)


def _cmd_extract(args: argparse.Namespace) -> int:
    """Look up an extraction point and open the wiki with it highlighted."""
    from . import db as dbmod
    from . import extracts as ex

    conn = dbmod.connect()
    term = " ".join(args.name)
    matches = ex.search(conn, term)
    if not matches:
        print(f"no extract matching {term!r}")
        print("Run 'uv run tarkov-tools import-templates --download' if the "
              "database has no extract data yet.")
        conn.close()
        return 1

    best = matches[0]
    side = (best.get("side") or "").replace("Pmc", "PMC").replace("Coop", "Co-op")
    print(f"\n{best['display_name']}   [{best['map_name']}]")
    print(f"  side          {side or '-'}")
    if best.get("chance") is not None:
        print(f"  chance        {best['chance']:.0f}%")
    if best.get("exfil_time"):
        print(f"  exfil time    {best['exfil_time']}s")
    requirement = ex.requirement_text(best)
    if requirement:
        print(f"  requirement   {requirement}")
    if best.get("entry_points"):
        print(f"  spawns        {best['entry_points']}")

    others = [m for m in matches[1:] if m["display_name"] != best["display_name"]]
    if others:
        print("\n  also matched: " + ", ".join(
            f"{m['display_name']} ({m['map_name']})" for m in others[:6]))

    url = ex.wiki_url(best)
    print(f"\n  {url}")

    if args.no_open:
        conn.close()
        return 0

    # Prefer an already-open tab for the same map over another duplicate.
    reuse_title = f"Map:{best.get('wiki_page')}" if best.get("wiki_page") else None
    opened, how = ex.open_in_browser(url, reuse_title=reuse_title)
    print(f"  {'opened in ' + how if opened else how}")
    conn.close()
    return 0 if opened else 1


def _cmd_hotkey(args: argparse.Namespace) -> int:
    """Show or change the popover hotkey."""
    from .config import LOCAL_CONFIG_PATH, set_local_override
    from .hotkey import HotkeyError, HotkeyListener, parse_hotkey

    current = load_config()["search"]["hotkey"]

    if not args.spec:
        print(f"current hotkey: {current}")
        print("\nchange it with:  uv run tarkov-tools hotkey <combo>")
        print("examples:        ctrl+t   ctrl+alt+k   ctrl+shift+space   rctrl+t   f9")
        print("\nA registered hotkey is claimed system-wide, so whatever you pick")
        print("stops reaching other applications while the popover is running.")
        return 0

    # Accept "ctrl+t", "ctrl + t" and "ctrl", "+", "t" alike, and store the
    # canonical form rather than whatever separators the shell handed over.
    parts = [
        part
        for chunk in args.spec
        for part in chunk.lower().replace(" ", "").split("+")
        if part
    ]
    spec = "+".join(parts)
    if not spec:
        print("no hotkey given", file=sys.stderr)
        return 1

    try:
        parse_hotkey(spec)
    except HotkeyError as exc:
        print(f"invalid hotkey: {exc}", file=sys.stderr)
        return 1

    # Actually claim it before saving, so a clash with another application
    # is reported now rather than the next time the popover starts.
    try:
        listener = HotkeyListener(spec, lambda: None)
        listener.start()
        listener.stop()
    except HotkeyError as exc:
        print(f"cannot use {spec!r}: {exc}", file=sys.stderr)
        print("Pick a different combination.", file=sys.stderr)
        return 1

    path = set_local_override("search", "hotkey", spec)
    print(f"hotkey: {current} -> {spec}")
    print(f"saved to {path.name}")
    if load_config()["search"]["hotkey"] != spec:
        print("warning: config did not take effect", file=sys.stderr)
        return 1
    print("\nRestart the popover for it to take effect.")
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    """Run the gamma watcher and the search popover together.

    One process, not two windows: tkinter must own the main thread, so the
    gamma watcher runs alongside it on a worker. Ctrl-C (or closing the
    popover) stops both, and gamma is restored on the way out.
    """
    import threading

    from . import gamma as gm
    from .popover import main as popover_main

    cfg = load_config()["gamma"]
    value = args.value if args.value is not None else cfg["value"]
    stop = threading.Event()

    def run_gamma() -> None:
        try:
            gm.watch(
                gamma=value,
                brightness=cfg["brightness"],
                contrast=cfg["contrast"],
                exes=tuple(cfg["exes"]),
                poll_seconds=cfg["poll_seconds"],
                game_monitor_only=cfg["game_monitor_only"] and not args.all_displays,
                companion_titles=tuple(cfg.get("companion_titles") or ()),
                companion_classes=tuple(cfg.get("companion_classes") or ()),
                revert_grace_seconds=cfg.get("revert_grace_seconds", 0.6),
                stop_event=stop,
                verbose=not args.quiet_gamma,
            )
        except Exception as exc:
            print(f"gamma watcher stopped: {exc}", file=sys.stderr)

    worker = threading.Thread(target=run_gamma, name="gamma", daemon=True)
    worker.start()

    try:
        return popover_main(args.hotkey)
    finally:
        stop.set()
        worker.join(timeout=3)
        print("gamma restored, both tools stopped")


# --- parser ------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tarkov-tools",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("gamma", help="display gamma, applied only while Tarkov has focus")
    g.add_argument("action", nargs="?", default="watch",
                   choices=["watch", "set", "reset", "displays"])
    g.add_argument("value", nargs="?", type=float, default=None, help="gamma value, e.g. 1.5")
    g.add_argument("--all-displays", action="store_true", help="apply to every monitor")
    g.add_argument("--unlock-range", action="store_true",
                   help="set GdiIcmGammaRange=256 so Windows stops clamping (needs admin)")
    g.set_defaults(func=_cmd_gamma)

    s = sub.add_parser("sync", help="build or refresh the local database from tarkov.dev")
    s.add_argument("--cache", action="store_true",
                   help="rebuild from the last downloaded responses instead of fetching")
    s.set_defaults(func=_cmd_sync)

    t = sub.add_parser(
        "import-templates",
        help="build the database from raw game item templates (no API needed)",
    )
    t.add_argument("--download", action="store_true",
                   help="fetch a template dump and locale file first")
    t.add_argument("--items", default=None, help="path to an existing items.json")
    t.add_argument("--locale", default=None, help="path to a locale en.json")
    t.set_defaults(func=_cmd_import_templates)

    q = sub.add_parser("search", help="look up a gun, round or magazine")
    q.add_argument("term", nargs="+")
    q.add_argument("--list", action="store_true", help="just list matches")
    q.set_defaults(func=_cmd_search)

    a = sub.add_parser("ammo", help="penetration chart")
    a.add_argument("caliber", nargs="?", default=None)
    a.add_argument("--list-calibers", action="store_true")
    a.set_defaults(func=_cmd_ammo)

    p = sub.add_parser("popover", help="hotkey-summoned search window")
    p.add_argument("--hotkey", default=None, help="override the configured hotkey")
    p.set_defaults(func=_cmd_popover)

    e = sub.add_parser(
        "extract", help="look up an extract and open the wiki map with it highlighted"
    )
    e.add_argument("name", nargs="+", help="extract name, e.g. 'zb-1011'")
    e.add_argument("--no-open", action="store_true", help="print details only")
    e.set_defaults(func=_cmd_extract)

    hk = sub.add_parser("hotkey", help="show or change the popover hotkey")
    hk.add_argument("spec", nargs="*", help="e.g. ctrl+t (omit to show the current one)")
    hk.set_defaults(func=_cmd_hotkey)

    st = sub.add_parser(
        "start", help="run the gamma watcher and the search popover together (default)"
    )
    st.add_argument("value", nargs="?", type=float, default=None, help="gamma value, e.g. 1.5")
    st.add_argument("--hotkey", default=None, help="override the configured hotkey")
    st.add_argument("--all-displays", action="store_true", help="gamma on every monitor")
    st.add_argument("--quiet-gamma", action="store_true", help="suppress gamma focus logging")
    st.set_defaults(func=_cmd_start)

    return parser


def main(argv: list[str] | None = None) -> int:
    write_default_config()
    if argv is None:
        argv = sys.argv[1:]
    # Bare `tarkov-tools` starts everything, which is the common case.
    if not argv:
        argv = ["start"]
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
