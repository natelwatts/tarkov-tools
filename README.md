# tarkov-tools

Local helpers for Escape from Tarkov: automatic display gamma, and a
hotkey-summoned search popover that answers *"what ammo, magazines and guns
actually go together"* without alt-tabbing to the wiki.

**Pure Python standard library. No pip installs, no build step.**

---

## What this does and does not touch

Nothing here reads, writes, or attaches to the game process.

| Component | How it works |
|---|---|
| Gamma | `SetDeviceGammaRamp` against a per-display device context. Same thing the NVIDIA Control Panel slider does. |
| Database | HTTP requests to a public API or a published template dump, cached into local SQLite. Read-only, and never during a raid. |
| Popover | An ordinary always-on-top window, like Notepad. No injection, no graphics-API hooking. |
| Hotkey | `RegisterHotKey`, which asks Windows to route one specific combination to us. Not a keyboard hook - it cannot see any other keystroke. |

There is no DLL injection, no `Present`/`vkQueuePresentKHR` hooking, and no
handle opened against the game beyond `PROCESS_QUERY_LIMITED_INFORMATION` to
read an executable *name* for focus detection.

That said: BSG has never affirmatively blessed this category of tool. Their
only official statement prohibits software that replaces, overrides, or
modifies game files or memory - none of which this does - but "not prohibited"
is not the same as "approved". Use your own judgement.

---

## Setup

There are two independent ways to build the database. Neither needs an API key.

### Option A - raw game templates (recommended, no API)

```powershell
python -m tarkov_tools.cli import-templates --download
```

Downloads a dump of the game's own item template database (~18 MB) plus the
English locale file (~2.9 MB) and imports in about a second. This is the
authoritative source: penetration, damage, ergonomics, magazine capacity and
the entire compatibility graph are the game's real numbers.

It carries **no market prices** - those do not exist in the game files.

### Option B - the tarkov.dev API

```powershell
python -m tarkov_tools.cli sync
```

Same static data, plus flea prices and trader offers.

> **If sync fails with `GraphQL server unavailable` / HTTP 422**, that is an
> outage on their side, not an auth problem - their Cloudflare Worker cannot
> reach its data backend. Auth failures would be 401/403. Retry later; the
> client already backs off and retries five times.

The two are designed to coexist. A template import never touches the price
columns, so you can refresh static data offline and layer prices on top with
`sync` whenever the API is reachable.

---

## Gamma

Applies your chosen gamma **only while Tarkov has focus**, and only on the
monitor the game window is on. The original ramp is always restored on exit -
via `finally`, `atexit`, and `SIGINT`/`SIGTERM` handlers - so a crash cannot
leave your desktop washed out.

```powershell
python -m tarkov_tools.cli gamma watch      # the one you want running
python -m tarkov_tools.cli gamma set 1.5    # apply right now
python -m tarkov_tools.cli gamma reset      # back to neutral
python -m tarkov_tools.cli gamma displays   # list monitors + current state
```

Or double-click `scripts\gamma-watch.cmd`.

Focus-triggered rather than launch-triggered on purpose: alt-tabbing to
Discord shouldn't leave your desktop blown out.

**On clamping.** Windows limits how far a gamma ramp may deviate from linear.
Gamma 1.5 sits comfortably inside the default limit and applies fine. If you
want something more extreme, run this once from an Administrator shell and
sign out and back in:

```powershell
python -m tarkov_tools.cli gamma --unlock-range
```

---

## Search

```powershell
python -m tarkov_tools.cli search m995        # full detail
python -m tarkov_tools.cli search stanag --list
python -m tarkov_tools.cli ammo               # penetration chart, all calibers
python -m tarkov_tools.cli ammo Caliber556x45NATO
python -m tarkov_tools.cli ammo --list-calibers
```

### The popover

```powershell
python -m tarkov_tools.cli popover
```

Press **Ctrl+Alt+T** (configurable) any time - including in game - and a search
box appears. Type, arrow through results, Esc to dismiss.

- Search a **gun** → every compatible round sorted by penetration, every
  magazine that fits sorted by capacity, and which traders sell it
- Search a **round** → every gun that fires it and every magazine that holds it
- Search a **magazine** → what it accepts and what accepts it

> Requires Tarkov in **borderless windowed** mode. Exclusive fullscreen will
> not composite another window on top.

---

## Where the data comes from

Two different kinds of data, with very different sources and lifetimes:

- **Static stats** (penetration, damage, ergonomics, magazine capacity, and
  crucially the compatibility lists) originate in the game's own item
  templates. They are the game's real numbers, not community measurements,
  and they only change when the game patches.
- **Flea prices** come from tarkov.dev's closed-source scanners, which page
  through live market listings. This is the only part that needs a live feed.

The compatibility graph this project is built around, and where each edge
comes from in the raw template format:

```
weapon._props.Chambers[]._props.filters[].Filter          -> weapon accepts ammo
weapon._props.Slots[_name=mod_magazine].filters[].Filter  -> weapon accepts magazine
magazine._props.Cartridges[]._props.filters[].Filter      -> magazine accepts ammo
```

All three edges come straight from the item templates, so "what fits what" is
authoritative rather than inferred. Items are classified by walking the
`_parent` chain to a root category (Weapon `5422acb9...`, Ammo `5485a868...`,
Magazine `5448bc23...`), which picks up every subcategory automatically.

Templates only carry internal names such as `patron_556x45_M995`; display
names live in a separate locale file keyed `"<id> Name"`, which is why the
importer wants both files.

### Changing the template source

Any dump in the raw BSG format works. Point `config.json` somewhere else if
the default mirror goes stale:

```jsonc
"templates": {
  "items_url":  "https://.../database/templates/items.json",
  "locale_url": "https://.../database/locales/global/en.json"
}
```

Or import files you already have on disk:

```powershell
python -m tarkov_tools.cli import-templates --items path\to\items.json --locale path\to\en.json
```

SPT (Single Player Tarkov) maintains the canonical dump, but at time of
writing its GitHub mirror keeps `items.json` behind git-lfs with an exhausted
LFS budget, and its own Gitea returns 410 - hence the plain-blob mirror in the
default config.

---

## Demo data

`scripts/make_demo_data.py` writes a small synthetic dataset in the exact shape
the API returns, so the code can be exercised while tarkov.dev is down.

**Every demo item is named `[DEMO] ...` and every stat in it is invented.**
Run a real `sync` to replace it with authoritative values.

```powershell
python scripts\make_demo_data.py
python -m tarkov_tools.cli sync --cache
```

---

## Configuration

`config.json` is generated on first run. Machine-specific overrides go in
`config.local.json`, which is gitignored and merged on top.

```jsonc
{
  "gamma":  { "value": 1.5, "exes": ["EscapeFromTarkov.exe"],
              "game_monitor_only": true },
  "api":    { "game_mode": "regular" },      // or "pve" - prices differ
  "search": { "hotkey": "ctrl+alt+t" }
}
```

---

## Layout

```
tarkov_tools/
  winapi.py    per-display gamma DCs, foreground process, monitor lookup
  gamma.py     ramp maths + focus watcher with guaranteed restore
  api.py       tarkov.dev GraphQL client, retry/backoff, response cache
  templates.py raw BSG item-template importer (no API required)
  db.py        SQLite schema, upserts, FTS5 index
  ingest.py    API -> database, builds the compatibility graph
  search.py    search + relationship queries
  hotkey.py    RegisterHotKey listener on its own message loop
  popover.py   tkinter search window
  cli.py       entry point
```

## Not built yet

The extract-map overlay: read the map name from the game's own log files
(`%LOCALAPPDATA%\Battlestate Games\EscapeFromTarkov\Logs`), OCR the extract
panel, fuzzy-match against the known extract list for that map, and draw the
tarkov.dev SVG with your extracts highlighted. The map SVGs and the
world-to-pixel transforms are already published.
