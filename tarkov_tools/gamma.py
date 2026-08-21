"""Display gamma control, applied only while Escape from Tarkov has focus.

This talks to the display driver via SetDeviceGammaRamp. It never touches
the game process. Setting gamma here is equivalent to moving the slider in
NVIDIA Control Panel by hand - the only thing this adds is automation.

The original ramp is ALWAYS restored on exit (normal, Ctrl-C, or crash).
"""

from __future__ import annotations

import atexit
import signal
import sys
import time
import winreg

from .winapi import (
    GammaRamp,
    display_for_window,
    exe_name_for_window,
    foreground_window,
    get_gamma_ramp,
    list_displays,
    set_gamma_ramp,
    toplevel_of,
    window_class,
    window_title,
)

DEFAULT_TARKOV_EXES = ("EscapeFromTarkov.exe",)

# Windows belonging to this toolkit that should NOT count as "focus lost".
# Opening the search popover moves focus away from the game, and without
# this the gamma would flip back and forth every time you summoned it.
#
# Both the class and the title must match. Requiring the Tk class avoids a
# false positive from, say, an Explorer window sitting on the repo folder,
# which would otherwise be titled "tarkov-tools" too.
DEFAULT_COMPANION_TITLES = ("Tarkov Tools",)
DEFAULT_COMPANION_CLASSES = ("TkTopLevel",)

# Windows clamps how far a gamma ramp may deviate from linear unless this
# registry value is set to 256. Without it, SetDeviceGammaRamp refuses
# large adjustments and the effect looks weaker than requested.
_ICM_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM"
_ICM_VALUE = "GdiIcmGammaRange"


def build_ramp(gamma: float, brightness: float = 0.0, contrast: float = 1.0) -> GammaRamp:
    """Build a 3x256 gamma ramp.

    gamma      >1.0 brightens midtones (1.5 is the common Tarkov value)
    brightness additive offset in [-1, 1], applied before the gamma curve
    contrast   multiplier around the midpoint, 1.0 is unchanged
    """
    if gamma <= 0:
        raise ValueError("gamma must be > 0")
    ramp = GammaRamp()
    for i in range(256):
        v = i / 255.0
        v = (v - 0.5) * contrast + 0.5 + brightness
        v = min(1.0, max(0.0, v))
        v = v ** (1.0 / gamma)
        value = min(65535, max(0, int(round(v * 65535.0))))
        ramp[0][i] = value
        ramp[1][i] = value
        ramp[2][i] = value
    return ramp


def gamma_range_unlocked() -> bool | None:
    """True if GdiIcmGammaRange is 256, False if restricted, None if unreadable."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ICM_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _ICM_VALUE)
            return int(value) == 256
    except FileNotFoundError:
        return False
    except OSError:
        return None


def unlock_gamma_range() -> bool:
    """Set GdiIcmGammaRange=256 so Windows stops clamping ramps.

    Requires administrator rights and a sign-out or reboot to take effect.
    """
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE, _ICM_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, _ICM_VALUE, 0, winreg.REG_DWORD, 256)
        return True
    except OSError:
        return False


class GammaController:
    """Applies a gamma ramp across displays and guarantees restoration.

    Captures each display's original ramp up front and restores it on exit,
    including via atexit so an unhandled crash still puts the screen back.
    """

    def __init__(
        self,
        gamma: float,
        brightness: float = 0.0,
        contrast: float = 1.0,
        displays: list[str] | None = None,
    ):
        self.gamma = gamma
        self.brightness = brightness
        self.contrast = contrast
        self.displays = displays or [name for name, _ in list_displays()]
        self._originals: dict[str, GammaRamp] = {}
        self.stale_baseline: list[str] = []

        # If a previous run died without restoring, the ramp we capture here
        # would BE the boosted ramp - and "restoring" it later would leave the
        # screen bright forever. Detect that by comparing against the ramp we
        # are about to apply, and fall back to neutral for those displays.
        target = build_ramp(gamma, brightness, contrast)
        for name in self.displays:
            try:
                current = get_gamma_ramp(name)
            except OSError:
                self._originals[name] = build_ramp(1.0)
                continue
            if current[0][128] == target[0][128] and target[0][128] != 32896:
                self.stale_baseline.append(name)
                self._originals[name] = build_ramp(1.0)
            else:
                self._originals[name] = current

        self._applied_on: set[str] = set()
        atexit.register(self.restore)

    def apply(self, displays: list[str] | None = None) -> dict[str, bool]:
        targets = displays if displays is not None else self.displays
        ramp = build_ramp(self.gamma, self.brightness, self.contrast)
        results: dict[str, bool] = {}
        for name in targets:
            ok = set_gamma_ramp(name, ramp)
            results[name] = ok
            if ok:
                self._applied_on.add(name)
        return results

    def restore(self) -> dict[str, bool]:
        """Put every touched display back, verifying the write actually took.

        A stuck-bright screen is the worst failure mode this tool could have,
        so the result is read back and retried rather than trusted.
        """
        results: dict[str, bool] = {}
        for name in list(self._applied_on):
            original = self._originals.get(name)
            neutral = build_ramp(1.0)
            ok = set_gamma_ramp(name, original if original is not None else neutral)
            if ok:
                try:
                    expected = (original if original is not None else neutral)[0][128]
                    if get_gamma_ramp(name)[0][128] != expected:
                        ok = set_gamma_ramp(name, neutral)
                except OSError:
                    pass
            if not ok:
                ok = set_gamma_ramp(name, neutral)
            results[name] = ok
            self._applied_on.discard(name)
        return results

    @property
    def active(self) -> bool:
        return bool(self._applied_on)

    @property
    def active_displays(self) -> set[str]:
        return set(self._applied_on)


def install_signal_handlers(controller: GammaController) -> None:
    """Restore gamma on Ctrl-C and on termination."""

    def _handler(signum, frame):
        controller.restore()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


def is_companion_window(hwnd, titles, classes) -> bool:
    """True if hwnd is one of our own tool windows.

    Focus moving here should not be treated as leaving the game.
    """
    top = toplevel_of(hwnd)
    return window_title(top) in titles and window_class(top) in classes


def watch(
    gamma: float = 1.5,
    brightness: float = 0.0,
    contrast: float = 1.0,
    exes: tuple[str, ...] = DEFAULT_TARKOV_EXES,
    poll_seconds: float = 0.35,
    game_monitor_only: bool = True,
    displays: list[str] | None = None,
    companion_titles: tuple[str, ...] = DEFAULT_COMPANION_TITLES,
    companion_classes: tuple[str, ...] = DEFAULT_COMPANION_CLASSES,
    revert_grace_seconds: float = 0.6,
    stop_event=None,
    verbose: bool = True,
) -> None:
    """Apply gamma whenever the game - or one of our own windows - has focus.

    Focus-triggered rather than launch-triggered, so the desktop is not left
    washed out while alt-tabbed to Discord or a browser.

    Our own search popover counts as "still in game" so that summoning it does
    not make the gamma flip back and forth. When it is focused, the gamma also
    stays pinned to the monitor the *game* is on, not the popover's monitor.

    revert_grace_seconds debounces any other brief focus change (an overlay
    toast, a click-through) so a momentary blip does not strobe the screen.
    """
    targets = {e.lower() for e in exes}
    companion_titles = tuple(companion_titles)
    companion_classes = tuple(companion_classes)
    controller = GammaController(gamma, brightness, contrast, displays)
    install_signal_handlers(controller)  # no-op off the main thread
    last_game_display: str | None = None
    pending_revert_since: float | None = None

    if verbose:
        print(f"displays   : {', '.join(controller.displays)}")
        print(f"watching   : {', '.join(exes)}")
        print(f"settings   : gamma={gamma} brightness={brightness} contrast={contrast}")
        scope = "game monitor only" if game_monitor_only else "all displays"
        print(f"scope      : {scope}")
        if controller.stale_baseline:
            print(f"note       : {', '.join(controller.stale_baseline)} already had this "
                  f"exact gamma applied\n             (leftover from a previous run?) - "
                  f"will restore to neutral instead")
        # Windows' default clamp still allows moderate ramps - 1.5 applies
        # fine. Only mention the unlock when the request is big enough to
        # actually risk rejection.
        if gamma >= 1.8 and gamma_range_unlocked() is False:
            print(
                "\nnote: gamma >= 1.8 with GdiIcmGammaRange unset may be clamped\n"
                "      by Windows. From an admin shell run:\n"
                "        python -m tarkov_tools.cli gamma --unlock-range\n"
                "      then sign out and back in for the full range.\n"
            )
        print("Ctrl-C to stop. Gamma is restored automatically on exit.")

    try:
        while not (stop_event is not None and stop_event.is_set()):
            hwnd = foreground_window()
            focused = (exe_name_for_window(hwnd) or "").lower()
            is_game = focused in targets
            is_companion = (not is_game) and is_companion_window(
                hwnd, companion_titles, companion_classes
            )

            if is_game or is_companion:
                pending_revert_since = None
                if game_monitor_only:
                    if is_game:
                        mon = display_for_window(hwnd)
                        if mon in controller.displays:
                            last_game_display = mon
                    # While the popover is focused, keep the gamma where the
                    # game is rather than following the popover's monitor.
                    mon = last_game_display
                    wanted = [mon] if mon in controller.displays else controller.displays
                else:
                    wanted = controller.displays

                if controller.active_displays != set(wanted):
                    controller.restore()
                    results = controller.apply(wanted)
                    if verbose:
                        good = [d for d, ok in results.items() if ok]
                        bad = [d for d, ok in results.items() if not ok]
                        who = focused if is_game else "search popover"
                        if good:
                            print(f"[+] {who} -> gamma {gamma} on {', '.join(good)}")
                        if bad:
                            print(f"[!] rejected on {', '.join(bad)} (ramp clamped)")

            elif controller.active:
                # Debounce: only revert once focus has been elsewhere for a
                # continuous grace period.
                now = time.monotonic()
                if pending_revert_since is None:
                    pending_revert_since = now
                elif now - pending_revert_since >= revert_grace_seconds:
                    controller.restore()
                    pending_revert_since = None
                    if verbose:
                        print("[-] focus lost -> gamma restored")

            if stop_event is not None:
                stop_event.wait(poll_seconds)
            else:
                time.sleep(poll_seconds)
    finally:
        controller.restore()
