"""Command line entry point.

    python -m tarkov_tools.cli gamma watch
    python -m tarkov_tools.cli gamma set 1.5
    python -m tarkov_tools.cli gamma reset
    python -m tarkov_tools.cli gamma displays
"""

from __future__ import annotations

import argparse
import sys

from .config import load_config, write_default_config


def _cmd_gamma(args: argparse.Namespace) -> int:
    from . import gamma as gm

    if args.unlock_range:
        current = gm.gamma_range_unlocked()
        if current:
            print("GdiIcmGammaRange is already 256 - no clamping.")
            return 0
        if gm.unlock_gamma_range():
            print("Set GdiIcmGammaRange=256. Sign out and back in for it to take effect.")
            return 0
        print(
            "Could not write the registry value. Re-run this command from an\n"
            "Administrator shell:  python -m tarkov_tools.cli gamma --unlock-range",
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
        # Applied deliberately and left in place, so drop the atexit restore.
        ctrl._applied_on.clear()
        for name, ok in results.items():
            print(f"{name}: {'gamma ' + str(value) if ok else 'REJECTED'}")
        return 0 if all(results.values()) else 1

    if args.action == "reset":
        from .winapi import list_displays, set_gamma_ramp

        neutral = gm.build_ramp(1.0)
        for name, _ in list_displays():
            ok = set_gamma_ramp(name, neutral)
            print(f"{name}: {'reset to 1.0' if ok else 'FAILED'}")
        return 0

    # default: watch
    gm.watch(
        gamma=value,
        brightness=cfg["brightness"],
        contrast=cfg["contrast"],
        exes=tuple(cfg["exes"]),
        poll_seconds=cfg["poll_seconds"],
        game_monitor_only=(
            cfg["game_monitor_only"] if args.all_displays is False else False
        ),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tarkov-tools", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("gamma", help="display gamma, applied only while Tarkov has focus")
    g.add_argument(
        "action",
        nargs="?",
        default="watch",
        choices=["watch", "set", "reset", "displays"],
        help="watch (default), set a value now, reset to 1.0, or list displays",
    )
    g.add_argument("value", nargs="?", type=float, default=None, help="gamma value, e.g. 1.5")
    g.add_argument("--all-displays", action="store_true", help="apply to every monitor")
    g.add_argument(
        "--unlock-range",
        action="store_true",
        help="set GdiIcmGammaRange=256 so Windows stops clamping (needs admin)",
    )
    g.set_defaults(func=_cmd_gamma)

    return parser


def main(argv: list[str] | None = None) -> int:
    write_default_config()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
