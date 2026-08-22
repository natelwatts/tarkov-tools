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
  uv run tarkov-tools prices update    pull current flea market prices
  uv run tarkov-tools stash            what you have and what it is worth
  uv run tarkov-tools popover          search popover only
  uv run tarkov-tools extract zb-1011  open the wiki map, extract highlighted
  uv run tarkov-tools tracker login    connect your TarkovTracker account
  uv run tarkov-tools hotkey ctrl+t    rebind the popover hotkey

'tt' is a shorter alias for 'tarkov-tools'.
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap

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


def _cmd_stash(args: argparse.Namespace) -> int:
    from . import db as dbmod
    from . import search as searchmod

    conn = dbmod.connect()
    list_name = "watch" if args.list == "watch" else "have"

    if args.clear:
        rows = searchmod.stash_contents(conn, list_name)
        if not rows:
            print(f"'{list_name}' was already empty")
            conn.close()
            return 0
        conn.execute("DELETE FROM stash WHERE list_name = ?", (list_name,))
        conn.commit()
        print(f"cleared {len(rows)} kinds off your '{list_name}' list")
        conn.close()
        return 0

    if args.item:
        words = list(args.item)
        count = 1
        if len(words) > 1 and words[-1].isdigit():
            count = int(words.pop())
        name = " ".join(words)
        matches = searchmod.search(conn, name, 5)
        matches = [m for m in matches if not str(m.get("id", "")).startswith("extract:")]
        if not matches:
            print(f"no item matching {name!r}")
            conn.close()
            return 1
        item = matches[0]
        held = searchmod.set_quantity(conn, item["id"], list_name, count)
        verb = f"holding {held}" if held else "removed from your stash"
        print(f"{item['name']}: {verb}")
        conn.close()
        return 0

    rows = searchmod.stash_contents(conn, list_name)
    if not rows:
        print(f"nothing on your '{list_name}' list yet.\n"
              f"Add something with:  uv run tarkov-tools stash \"ledx\" 3\n"
              f"or press Ctrl+Shift+H in the overlay.")
        conn.close()
        return 0

    print(f"\n{'qty':>4}  {'each':>11}  {'total':>12}  item")
    total = 0
    unpriced = 0
    for row in rows:
        total += row["line_value"] or 0
        each = f"{row['unit_price']:,}" if row["unit_price"] else "-"
        if not row["unit_price"]:
            unpriced += 1
        line = f"{row['line_value']:,}" if row["line_value"] else "-"
        print(f"{row['quantity']:>4}  {each:>11}  {line:>12}  {row['name'][:44]}")

    kinds = len(rows)
    units = sum(r["quantity"] for r in rows)
    print(f"\n{kinds} kinds, {units} items, {total:,} RUB at flea prices")
    if unpriced:
        print(f"({unpriced} of them have no flea price - banned, or no snapshot yet)")
    conn.close()
    return 0


def _cmd_prices(args: argparse.Namespace) -> int:
    from . import db as dbmod
    from . import prices as pricesmod

    conn = dbmod.connect()

    if args.action == "update":
        print(f"fetching {pricesmod.FEED_URL}")
        try:
            counts = pricesmod.update(conn)
        except Exception as exc:
            print(f"could not fetch prices: {exc}", file=sys.stderr)
            conn.close()
            return 1
        print(f"  {counts['in_feed']} items priced, "
              f"{counts['matched_our_items']} of them in your database")

    if args.action == "top":
        rows = pricesmod.top_by_slot(conn, args.limit, args.min_price)
        if not rows:
            print("no prices yet - run 'tarkov-tools prices update'")
            conn.close()
            return 1
        print(f"\n{'value/slot':>12}  {'price':>11}  size  item")
        for row in rows:
            size = f"{row['width']}x{row['height']}"
            print(f"{row['per_slot']:>12,}  {row['price']:>11,}  {size:<4}  {row['name'][:44]}")

    age = pricesmod.snapshot_age(conn)
    priced = conn.execute("SELECT COUNT(*) FROM flea_prices").fetchone()[0]
    print()
    if age is None:
        print("no price snapshot yet - run 'tarkov-tools prices update'")
    else:
        print(f"{priced} items priced, snapshot is {age / 60:.0f} min old")
    conn.close()
    return 0


BUILD_HINT = ("The item database is empty. Build it with:\n"
              "  uv run tarkov-tools import-templates --download")


def _database_is_empty(conn) -> bool:
    """True when nothing has been imported yet.

    Worth distinguishing from a genuine miss: on a fresh clone every lookup
    returns nothing, and "no matches" reads like the tool is broken rather
    than like a step was skipped.
    """
    try:
        return not conn.execute("SELECT 1 FROM items LIMIT 1").fetchone()
    except Exception:
        return True


def _cmd_search(args: argparse.Namespace) -> int:
    from . import db as dbmod
    from . import search as searchmod

    conn = dbmod.connect()
    term = " ".join(args.term)
    results = searchmod.search(conn, term, load_config()["search"]["max_results"])
    if not results:
        print(BUILD_HINT if _database_is_empty(conn) else f"no matches for {term!r}")
        conn.close()
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
    flea = data.get("flea")
    if flea:
        line = f"  flea {money(flea['price'])} RUB"
        if flea.get("per_slot"):
            line += f"   {money(flea['per_slot'])}/slot"
            if (flea.get("slots") or 1) > 1:
                line += f" ({flea['slots']} slots)"
        if abs(flea.get("change_pct") or 0) >= 0.5:
            line += f"   {flea['change_pct']:+.0f}%"
        print(line)
    elif item.get("avg_24h_price"):
        print(f"  flea {money(item['avg_24h_price'])} RUB")
    else:
        print("  not on the flea market")

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
        print(BUILD_HINT if _database_is_empty(conn)
              else "no ammo found - check the caliber name")
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
    reuse_title = ex.map_title(best["wiki_page"]) if best.get("wiki_page") else None
    opened, how = ex.open_in_browser(url, reuse_title=reuse_title)
    print(f"  {'opened in ' + how if opened else how}")

    cfg = load_config().get("extracts", {})
    if opened and cfg.get("apply_map_filters", True) and best.get("wiki_page"):
        if ex.prepare_map(
            best["wiki_page"],
            cfg.get("categories"),
            verbose=True,
            fullscreen=cfg.get("fullscreen_map", True),
            unzoom=cfg.get("zoom_out_map", True),
            marker_name=best.get("display_name"),
        ):
            print("  map filtered to the categories you care about")
    conn.close()
    return 0 if opened else 1


def _cmd_tracker(args: argparse.Namespace) -> int:
    """Connect a TarkovTracker account and check the connection."""
    from . import tracker as tt

    if args.action == "login":
        # Prefer stdin or the environment so the token does not end up in
        # shell history or a process listing.
        token = (args.token or os.environ.get("TARKOVTRACKER_TOKEN") or "").strip()
        if not token and not sys.stdin.isatty():
            token = sys.stdin.read().strip()
        if not token:
            print(
                "No token given. Any of these work:\n"
                "  uv run tarkov-tools tracker login --token PVP_xxxxx\n"
                '  $env:TARKOVTRACKER_TOKEN="PVP_xxxxx"; uv run tarkov-tools tracker login\n'
                '  "PVP_xxxxx" | uv run tarkov-tools tracker login',
                file=sys.stderr,
            )
            return 1
        if not token.startswith(tt.TOKEN_PREFIXES):
            print(f"warning: token does not start with one of "
                  f"{', '.join(tt.TOKEN_PREFIXES)} - legacy tt_ tokens no longer work.")
        try:
            print("verifying with TarkovTracker ...")
            summary = tt.describe_token(token)
        except tt.TrackerError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        tt.save_token(token)
        print(f"\n{summary}")
        print(f"\nsaved to {tt.config_path_hint()}  (gitignored)")
        return 0

    token = tt.load_token()
    if not token:
        print("No TarkovTracker token saved yet.")
        print("  uv run tarkov-tools tracker login --token PVP_xxxxx")
        return 1

    if args.action == "status":
        try:
            print(tt.describe_token(token))
        except tt.TrackerError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0

    if args.action == "logout":
        tt.save_token("")
        print(f"token cleared from {tt.config_path_hint()}")
        return 0

    if args.action == "sync":
        from . import db as dbmod

        conn = dbmod.connect()
        try:
            with conn:
                tt.sync_needed(conn, token)
        except tt.TrackerError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        return 0

    if args.action == "needed":
        from . import db as dbmod

        conn = dbmod.connect()
        tt.ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT n.item_id, i.name, SUM(n.need - n.have) AS outstanding,
                   MAX(n.found_in_raid) AS fir, COUNT(*) AS sources,
                   MAX(n.available) AS available
            FROM needed_items n LEFT JOIN items i ON i.id = n.item_id
            WHERE n.optional = 0 AND n.need > n.have AND n.alternatives <= ?
            GROUP BY n.item_id
            ORDER BY available DESC, outstanding DESC
            LIMIT ?
            """,
            (args.max_alternatives, args.limit),
        ).fetchall()
        if not rows:
            print("Nothing recorded. Run 'tracker sync' first.")
            return 1
        print(f"\n{'need':>5}  {'FIR':<4} {'for':<3} item")
        for r in rows:
            flag = "FIR" if r["fir"] else ""
            soon = "" if r["available"] else "  (locked)"
            print(f"{r['outstanding']:>5}  {flag:<4} {r['sources']:<3} "
                  f"{r['name'] or r['item_id']}{soon}")
        conn.close()
        return 0

    return 0


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

def _sub(subparsers, name: str, summary: str, description: str,
         examples: str = "") -> argparse.ArgumentParser:
    """A subcommand whose --help explains itself to someone who did not build it."""
    return subparsers.add_parser(
        name,
        help=summary,
        # Dedent before stripping: these are written as indented triple-quoted
        # blocks, and the raw formatter would print that indentation as-is.
        description=textwrap.dedent(description).strip(),
        epilog=textwrap.dedent(examples).strip("\n").rstrip() or None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tarkov-tools",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    g = _sub(
        sub, "gamma",
        "display gamma, applied only while Tarkov has focus",
        """
        Raise display gamma so dark corners are readable, without leaving your
        whole desktop washed out.

        'watch' is the useful one: it waits for Tarkov, raises gamma on the
        monitor the game is actually on, and puts it back the moment focus
        leaves. The search popover counts as the game for this purpose, so
        summoning it does not drop you back to a dark screen.

        Windows clamps how far gamma can be pushed unless a registry value is
        set; --unlock-range does that, and needs an administrator shell.
        """,
        """
examples:
  uv run tarkov-tools gamma watch          apply while Tarkov has focus
  uv run tarkov-tools gamma set 1.5        set it now and leave it
  uv run tarkov-tools gamma reset          back to 1.0
  uv run tarkov-tools gamma displays       list monitors and their gamma
        """,
    )
    g.add_argument("action", nargs="?", default="watch",
                   choices=["watch", "set", "reset", "displays"],
                   help="default: watch")
    g.add_argument("value", nargs="?", type=float, default=None, help="gamma value, e.g. 1.5")
    g.add_argument("--all-displays", action="store_true", help="apply to every monitor")
    g.add_argument("--unlock-range", action="store_true",
                   help="set GdiIcmGammaRange=256 so Windows stops clamping (needs admin)")
    g.set_defaults(func=_cmd_gamma)

    s = _sub(
        sub, "sync",
        "build or refresh the local database from tarkov.dev",
        """
        Fill the local database from the tarkov.dev GraphQL API, which also
        brings in trader prices.

        That API is not always up. If it fails, use 'import-templates'
        instead - it builds the same gun, ammo, magazine and part data
        straight from the game's own files and needs no API at all.
        """,
        """
examples:
  uv run tarkov-tools sync
  uv run tarkov-tools sync --cache         rebuild from the last download
        """,
    )
    s.add_argument("--cache", action="store_true",
                   help="rebuild from the last downloaded responses instead of fetching")
    s.set_defaults(func=_cmd_sync)

    t = _sub(
        sub, "import-templates",
        "build the database from raw game item templates (no API needed)",
        """
        Build the database from the game's own item templates. This is the
        one to run first, and the one to fall back on whenever tarkov.dev is
        down: no API key, no account, nothing to sign up for.

        It brings in every item, its attachment slots and what fits them.
        The only thing it cannot know is trader prices.
        """,
        """
examples:
  uv run tarkov-tools import-templates --download    first run - fetches everything
  uv run tarkov-tools import-templates --items items.json --locale en.json
        """,
    )
    t.add_argument("--download", action="store_true",
                   help="fetch a template dump and locale file first")
    t.add_argument("--items", default=None, help="path to an existing items.json")
    t.add_argument("--locale", default=None, help="path to a locale en.json")
    t.set_defaults(func=_cmd_import_templates)

    q = _sub(
        sub, "search",
        "look up a gun, round or magazine",
        """
        Look something up without leaving the terminal. A gun lists the ammo
        and magazines it takes, a round lists the guns that fire it, a part
        lists the weapons it fits.

        This is the same lookup the popover does; the popover is usually what
        you want in game.
        """,
        """
examples:
  uv run tarkov-tools search m995
  uv run tarkov-tools search mp5 --list
        """,
    )
    q.add_argument("term", nargs="+", help="name or part of one, e.g. 'm995'")
    q.add_argument("--list", action="store_true", help="just list matches")
    q.set_defaults(func=_cmd_search)

    a = _sub(
        sub, "ammo",
        "penetration chart",
        """
        Every round of a caliber, ordered by penetration, with damage and
        armour damage alongside - the table worth glancing at before a raid.
        """,
        """
examples:
  uv run tarkov-tools ammo 5.56x45
  uv run tarkov-tools ammo --list-calibers
        """,
    )
    a.add_argument("caliber", nargs="?", default=None,
                   help="e.g. 5.56x45 (omit for all)")
    a.add_argument("--list-calibers", action="store_true",
                   help="just show which calibers exist")
    a.set_defaults(func=_cmd_ammo)

    p = _sub(
        sub, "popover",
        "hotkey-summoned search window",
        """
        The search window on its own, without the gamma watcher. Press the
        hotkey - Ctrl+T by default - and it appears over the game.

        Tarkov must be in borderless windowed mode; nothing composites over
        exclusive fullscreen.
        """,
        """
examples:
  uv run tarkov-tools popover
  uv run tarkov-tools popover --hotkey ctrl+alt+k
        """,
    )
    p.add_argument("--hotkey", default=None, help="override the configured hotkey")
    p.set_defaults(func=_cmd_popover)

    e = _sub(
        sub, "extract",
        "look up an extract and open the wiki map with it highlighted",
        """
        Show an extract's side, chance, timer and requirement, then open the
        wiki's interactive map with that exit selected - expanded to fill the
        page, zoomed out to the whole map, and filtered down to extractions
        and PMC spawns.

        An already-open tab for the same map is reused rather than piling up
        duplicates.
        """,
        """
examples:
  uv run tarkov-tools extract zb-1011
  uv run tarkov-tools extract old gas station
  uv run tarkov-tools extract dorms --no-open      details only
        """,
    )
    e.add_argument("name", nargs="+", help="extract name, e.g. 'zb-1011'")
    e.add_argument("--no-open", action="store_true", help="print details only")
    e.set_defaults(func=_cmd_extract)

    tr = _sub(
        sub, "tracker",
        "connect a TarkovTracker account",
        """
        Optional. Connect a TarkovTracker account and the popover gains a
        'Needed' filter showing what your quests and hideout still want.

        Everything else works without this. The token is stored in
        config.local.json, which is git-ignored, is only ever shown masked,
        and is used read-only - nothing here writes to your account.
        """,
        """
examples:
  uv run tarkov-tools tracker login                 prompts for the token
  uv run tarkov-tools tracker login --token PVP_xxx
  uv run tarkov-tools tracker status                is it connected?
  uv run tarkov-tools tracker sync                  refresh what is needed
  uv run tarkov-tools tracker needed                list it
  uv run tarkov-tools tracker logout                forget the token
        """,
    )
    tr.add_argument("action", nargs="?", default="status",
                    choices=["login", "status", "logout", "sync", "needed"],
                    help="default: status")
    tr.add_argument("--token", default=None,
                    help="API token; omit to read stdin or $TARKOVTRACKER_TOKEN")
    tr.add_argument("--limit", type=int, default=40, help="rows for 'needed'")
    tr.add_argument("--max-alternatives", type=int, default=3,
                    help="hide 'any of N' objectives wider than this")
    tr.set_defaults(func=_cmd_tracker)

    pr = _sub(
        sub, "prices",
        "flea market prices, and what is worth the space",
        """
        Pull current flea market prices so the search shows what things sell
        for - the question worth answering mid-raid.

        Prices come from tarkovforge's public snapshot, which is refreshed
        hourly and keyed by the same item ids this database already uses.
        That is a stand-in for tarkov.dev's API, which is the canonical
        source but has been down; nothing here needs an account either way.

        Items with no price are not missing - they are banned from the flea.

        'top' ranks by value per slot rather than by price, which is what
        decides what comes home. Weapons are left out of that ranking: a
        built gun's footprint depends on its attachments, so the template
        size would make the number wrong.
        """,
        """
examples:
  uv run tarkov-tools prices update            pull the latest snapshot
  uv run tarkov-tools prices top               best value per slot
  uv run tarkov-tools prices top --min-price 50000
  uv run tarkov-tools prices                   how fresh is what I have?
        """,
    )
    pr.add_argument("action", nargs="?", default="status",
                    choices=["status", "update", "top"], help="default: status")
    pr.add_argument("--limit", type=int, default=30, help="rows for 'top'")
    pr.add_argument("--min-price", type=int, default=0,
                    help="ignore items cheaper than this in 'top'")
    pr.set_defaults(func=_cmd_prices)

    sh = _sub(
        sub, "stash",
        "what you have, and what it is worth",
        """
        Everything you have marked as being in your stash, with how many of
        each, ordered by what the pile is worth.

        Marking happens in the overlay - Ctrl+H for one, Ctrl+Shift+H to type
        a count, Ctrl+Up/Down to nudge it - but a count can be set from here
        too, which is easier when you are stocktaking rather than playing.

        Totals use flea prices, so run 'prices update' first if they look
        stale. Items banned from the flea count as zero rather than being
        left out.
        """,
        """
examples:
  uv run tarkov-tools stash                    everything you hold
  uv run tarkov-tools stash ledx 3             set a count
  uv run tarkov-tools stash ledx 0             remove it
  uv run tarkov-tools stash --clear            empty the list
  uv run tarkov-tools stash --list watch       the watch list instead
        """,
    )
    # One greedy positional: a trailing number is read as the count, because
    # argparse cannot split "ledx 3" between a nargs="*" and a nargs="?".
    sh.add_argument("item", nargs="*",
                    help="item name, optionally followed by a count (0 removes)")
    sh.set_defaults(func=_cmd_stash)
    sh.add_argument("--list", default="have", choices=["have", "watch"],
                    help="which list (default: have)")
    sh.add_argument("--clear", action="store_true",
                    help="empty the whole list")

    hk = _sub(
        sub, "hotkey",
        "show or change the popover hotkey",
        """
        Change the key that summons the search window. The binding is claimed
        system-wide while the tool runs, so pick something the game and your
        browser do not need - Ctrl+T is 'new tab' in every browser.

        A modifier can be side-specific: 'rctrl+t' fires only on the right
        Ctrl, leaving the left one alone.
        """,
        """
examples:
  uv run tarkov-tools hotkey               show the current binding
  uv run tarkov-tools hotkey ctrl+t
  uv run tarkov-tools hotkey rctrl+alt+k
        """,
    )
    hk.add_argument("spec", nargs="*", help="e.g. ctrl+t (omit to show the current one)")
    hk.set_defaults(func=_cmd_hotkey)

    st = _sub(
        sub, "start",
        "run the gamma watcher and the search popover together (default)",
        """
        Everything at once, and what you want bound to a shortcut: the gamma
        watcher and the search popover in one window. Ctrl-C stops both and
        restores gamma.

        This is what plain 'uv run tarkov-tools' does.
        """,
        """
examples:
  uv run tarkov-tools                      same thing
  uv run tarkov-tools start 1.6            with an explicit gamma value
  uv run tarkov-tools start --quiet-gamma  without the focus logging
        """,
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
