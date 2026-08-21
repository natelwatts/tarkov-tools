"""Configuration loading.

config.json holds committed defaults. config.local.json, if present, is
merged on top and is gitignored - put machine-specific overrides there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.json"
LOCAL_CONFIG_PATH = REPO_ROOT / "config.local.json"
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "tarkov.sqlite3"

DEFAULTS: dict[str, Any] = {
    "gamma": {
        "value": 1.5,
        "brightness": 0.0,
        "contrast": 1.0,
        "exes": ["EscapeFromTarkov.exe"],
        "poll_seconds": 0.35,
        "game_monitor_only": True,
        # Our own windows that should not count as "focus left the game".
        "companion_titles": ["Tarkov Tools"],
        "companion_classes": ["TkTopLevel"],
        "revert_grace_seconds": 0.6,
    },
    "api": {
        "endpoint": "https://api.tarkov.dev/graphql",
        "game_mode": "regular",
        "language": "en",
        "timeout_seconds": 60,
        "max_retries": 5,
    },
    "templates": {
        # Raw BSG item templates. Any dump in the same format works; point
        # these at a different mirror if this one goes stale or offline.
        "items_url": (
            "https://raw.githubusercontent.com/paulov-t/Paulov.Tarkov.Db/master"
            "/database/templates/items.json"
        ),
        "locale_url": (
            "https://raw.githubusercontent.com/paulov-t/Paulov.Tarkov.Db/master"
            "/database/locales/global/en.json"
        ),
    },
    "extracts": {
        # After the wiki map opens, hide every category except these.
        # The map has no filter URL parameter, so its sidebar is clicked;
        # set this false to leave the map exactly as the wiki renders it.
        "apply_map_filters": True,
        "categories": [
            ["Extractions", "PMC"],
            ["Extractions", "Scav"],
            ["Spawns", "PMC"],
        ],
    },
    "search": {
        "hotkey": "ctrl+t",
        "max_results": 40,
        # [x, y] of the popover, updated when you drag it. null = centred.
        "position": None,
        # Filter chip order, by label. Empty means the built-in order.
        # Ctrl+Shift+Left/Right in the popover rewrites this.
        "filter_order": [],
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULTS))
    for path in (CONFIG_PATH, LOCAL_CONFIG_PATH):
        if path.exists():
            try:
                config = _deep_merge(config, json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path.name} is not valid JSON: {exc}") from exc
    return config


def write_default_config() -> Path:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
    return CONFIG_PATH


def set_local_override(section: str, key: str, value: Any) -> Path:
    """Persist one setting to config.local.json.

    Overrides live in the local file rather than config.json so that personal
    settings are not committed and are never clobbered by an update to the
    shipped defaults.
    """
    current: dict[str, Any] = {}
    if LOCAL_CONFIG_PATH.exists():
        try:
            current = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"{LOCAL_CONFIG_PATH.name} is not valid JSON: {exc}"
            ) from exc
    current.setdefault(section, {})[key] = value
    LOCAL_CONFIG_PATH.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return LOCAL_CONFIG_PATH
