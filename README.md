# tarkov-tools

Local helpers for Escape from Tarkov: automatic display gamma, and a
hotkey-summoned search popover that answers *"what ammo, magazines and guns
actually go together"* without alt-tabbing to the wiki.

**Pure Python standard library - zero dependencies. Managed with [uv](https://docs.astral.sh/uv/).**

---

## What this does and does not touch

Nothing here reads, writes, or attaches to the game process.

| Component | How it works |
|---|---|
| Gamma | `SetDeviceGammaRamp` against a per-display device context. Same thing the NVIDIA Control Panel slider does. |
| Database | HTTP requests to a public API or a published template dump, cached into local SQLite. Read-only, and never during a raid. |
| Popover | An ordinary always-on-top window, like Notepad. No injection, no graphics-API hooking. |
| Hotkey | `RegisterHotKey`, which asks Windows to route one specific combination to us. Not a keyboard hook - it cannot see any other keystroke. |
| Focus | Reads the foreground window's title/class and executable *name* only, to know whether the game is in front. |

There is no DLL injection, no `Present`/`vkQueuePresentKHR` hooking, and no
handle opened against the game beyond `PROCESS_QUERY_LIMITED_INFORMATION` to
read an executable *name* for focus detection.

That said: BSG has never affirmatively blessed this category of tool. Their
only official statement prohibits software that replaces, overrides, or
modifies game files or memory - none of which this does - but "not prohibited"
is not the same as "approved". Use your own judgement.

---

## Quick start

```powershell
git clone <this repo>
cd tarkov-tools
uv sync                                          # creates .venv, no dependencies to fetch
uv run tarkov-tools import-templates --download  # build the item database
uv run tarkov-tools                              # start everything
```

That last command runs **both tools in one process**: the gamma watcher and
the search popover. Ctrl-C (or closing the window) stops both and restores
your gamma. `scripts	arkov-tools.cmd` does the same by double-click.

`tt` is a shorter alias for `tarkov-tools`.

| Command | What it does |
|---|---|
| `uv run tarkov-tools` | gamma watcher + popover together (same as `start`) |
| `uv run tarkov-tools start 1.6` | same, overriding the gamma value |
| `uv run tarkov-tools gamma watch` | gamma only |
| `uv run tarkov-tools popover` | popover only |
| `uv run tarkov-tools search m995` | one-off lookup in the terminal |
| `uv run tarkov-tools ammo` | penetration chart |
| `uv run tarkov-tools hotkey ctrl+alt+k` | rebind the popover hotkey |
| `uv run tarkov-tools import-templates --download` | rebuild the database |

---

## Building the database

There are two independent ways to build it. Neither needs an API key.

### Option A - raw game templates (recommended, no API)

```powershell
uv run tarkov-tools import-templates --download
```

Downloads a dump of the game's own item template database (~18 MB) plus the
English locale file (~2.9 MB) and imports in about a second. This is the
authoritative source: penetration, damage, ergonomics, magazine capacity and
the entire compatibility graph are the game's real numbers.

It carries **no market prices** - those do not exist in the game files.

### Option B - the tarkov.dev API

```powershell
uv run tarkov-tools sync
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
uv run tarkov-tools gamma watch      # the one you want running
uv run tarkov-tools gamma set 1.5    # apply right now
uv run tarkov-tools gamma reset      # back to neutral
uv run tarkov-tools gamma displays   # list monitors + current state
```

Or run `uv run tarkov-tools` to start it alongside the popover.

Focus-triggered rather than launch-triggered on purpose: alt-tabbing to
Discord shouldn't leave your desktop blown out.

**On clamping.** Windows limits how far a gamma ramp may deviate from linear.
Gamma 1.5 sits comfortably inside the default limit and applies fine. If you
want something more extreme, run this once from an Administrator shell and
sign out and back in:

```powershell
uv run tarkov-tools gamma --unlock-range
```

---

## Search

```powershell
uv run tarkov-tools search m995        # full detail
uv run tarkov-tools search stanag --list
uv run tarkov-tools ammo               # penetration chart, all calibers
uv run tarkov-tools ammo Caliber556x45NATO
uv run tarkov-tools ammo --list-calibers
```

### The popover

```powershell
uv run tarkov-tools popover
```

Press **Ctrl+T** (configurable) any time - including in game - and a search
box appears. Type, arrow through results, Esc to dismiss.

The gamma watcher recognises this window by title *and* window class, so
summoning the popover does not read as "you left the game": the gamma stays
applied, and stays on the monitor the **game** is on rather than following
the popover.

#### Rebinding

```powershell
uv run tarkov-tools hotkey                  # show the current binding
uv run tarkov-tools hotkey ctrl+alt+k       # change it
```

The new combination is registered for real before it is saved, so a clash
with another application is reported immediately rather than silently
failing the next time the popover starts. The setting is written to
`config.local.json`, which is gitignored, so personal bindings are never
committed and survive updates to the shipped defaults.

Accepted forms: `ctrl+t`, `ctrl + t`, `f9`, `ctrl+shift+space`, `win+k`.
Modifiers may be side-specific - `rctrl+t` fires only on the RIGHT Ctrl,
done by checking that one key's state when the hotkey fires rather than by
installing a keyboard hook. `--hotkey` overrides for a single run without
saving.

> A registered hotkey is claimed system-wide, so while the popover is running
> **the combination stops reaching other applications**. Ctrl+T is "new tab"
> in every browser, so expect that to stop working until you exit. `ctrl+alt+t`
> or `ctrl+shift+space` are far less contested if that becomes annoying.

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
uv run tarkov-tools import-templates --items path\to\items.json --locale path\to\en.json
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
uv run python scripts\make_demo_data.py
uv run tarkov-tools sync --cache
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
  "search": { "hotkey": "ctrl+t" }
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
  cli.py       entry point ('start' runs gamma + popover in one process)
```

## Not built yet

The extract-map overlay: read the map name from the game's own log files
(`%LOCALAPPDATA%\Battlestate Games\EscapeFromTarkov\Logs`), OCR the extract
panel, fuzzy-match against the known extract list for that map, and draw the
tarkov.dev SVG with your extracts highlighted. The map SVGs and the
world-to-pixel transforms are already published.
