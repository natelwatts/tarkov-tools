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
        "poll_seconds": 1.0,
        "game_monitor_only": True,
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
    "search": {
        "hotkey": "ctrl+alt+t",
        "max_results": 40,
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
