"""Command line entry point for tarkov-tools.

    python -m tarkov_tools.cli gamma watch      apply gamma while Tarkov has focus
    python -m tarkov_tools.cli gamma set 1.5    set it now
    python -m tarkov_tools.cli gamma reset      back to 1.0
    python -m tarkov_tools.cli gamma displays   list monitors and current gamma

    python -m tarkov_tools.cli sync             build/refresh the local database
    python -m tarkov_tools.cli search m995      look something up in the terminal
    python -m tarkov_tools.cli ammo             penetration chart by caliber
    python -m tarkov_tools.cli popover          hotkey-summoned search window
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

    return parser


def main(argv: list[str] | None = None) -> int:
    write_default_config()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
