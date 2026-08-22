# tarkov-tools

Local helpers for Escape from Tarkov: automatic display gamma, and a
hotkey-summoned search popover that answers *"what ammo, magazines and guns
actually go together"* without alt-tabbing to the wiki.

**Almost entirely the Python standard library. Managed with [uv](https://docs.astral.sh/uv/).**

The one dependency is `comtypes` (small, pure Python), used only to find an
already-open Chrome tab. Everything else is stdlib, and the tool degrades
gracefully if it is missing.

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

You need **Windows**, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and **Chrome** if you want the wiki and map features. Python itself comes from
uv, so there is nothing else to install.

```powershell
git clone git@github.com:natelwatts/tarkov-tools.git
cd tarkov-tools
uv sync                                          # creates .venv, no dependencies to fetch
uv run tarkov-tools import-templates --download  # build the item database
uv run tarkov-tools                              # start everything
```

That last command runs **both tools in one process**: the gamma watcher and
the search popover. Ctrl-C (or closing the window) stops both and restores
your gamma. `scripts\tarkov-tools.cmd` does the same by double-click.

`tt` is a shorter alias for `tarkov-tools`.

| Command | What it does |
|---|---|
| `uv run tarkov-tools` | gamma watcher + popover together (same as `start`) |
| `uv run tarkov-tools start 1.6` | same, overriding the gamma value |
| `uv run tarkov-tools gamma watch` | gamma only |
| `uv run tarkov-tools popover` | popover only |
| `uv run tarkov-tools search m995` | one-off lookup in the terminal |
| `uv run tarkov-tools ammo` | penetration chart |
| `uv run tarkov-tools prices update` | pull current flea market prices |
| `uv run tarkov-tools extract zb-1011` | open the interactive map on that extract |
| `uv run tarkov-tools hotkey ctrl+alt+k` | rebind the popover hotkey |
| `uv run tarkov-tools import-templates --download` | rebuild the database |

**Every command explains itself** - `uv run tarkov-tools <command> --help` has
a description and worked examples, and `uv run tarkov-tools --help` lists them
all.

---

## Flea market prices

Search anything and the detail pane leads with what it sells for, what that
works out to **per inventory slot**, and which way the price has moved:

```
Salewa first aid kit
  flea 37,972 RUB   18,986/slot (2 slots)   +1%
```

Per slot is the number that decides what comes home - a 200k item filling six
slots loses to a 90k item filling one. It is left off weapons on purpose: a
built gun's footprint depends on what is bolted to it, so the item template's
size would make that number confidently wrong.

```powershell
uv run tarkov-tools prices update      # pull the latest snapshot
uv run tarkov-tools prices top         # best value per slot
uv run tarkov-tools prices             # how fresh is what I have?
```

The popover refreshes prices by itself when it starts and whenever you press
F5, so what you see mid-raid is current to the hour without touching a
terminal.

**Where the numbers come from.** tarkov.dev's GraphQL API is the community's
canonical price source and has been returning *"GraphQL server unavailable"*.
So prices come from [tarkovforge](https://tarkovforge.com/market)'s public
snapshot instead - one static JSON file, refreshed hourly, keyed by the same
BSG item ids this database already uses, so it joins straight on with no name
matching. It is derived from tarkov.dev, so it is the same data one step
removed.

**Items with no price are not missing** - they are the ones banned from the
flea market, and the tool says so rather than showing a blank.

### When an item has nothing

Of 4,613 items, 3,379 have a market page and the rest split two ways, so
Ctrl+Shift+Enter behaves differently depending on which:

| | market page | flea price | what happens |
|---|---|---|---|
| 3,379 items | yes | either | opens the item's page |
| 351 items | no | yes | opens the market search - it *is* tradeable, their site just has no page |
| 883 items | no | no | says **"not traded on the flea market"** and stays put |

That last case is quest items, ammo boxes and intel. Opening a market search
for them would show an empty table, so the overlay explains why instead of
hiding itself behind a dead end - and points out that Ctrl+Enter still opens
the wiki, which does have something to say about them.

**What is not here yet: which trader pays the most.** That needs per-trader
sell prices, which only tarkov.dev has - tarkovforge reads them live from the
same API rather than publishing them, and the raw item templates carry no
prices at all. The display for it is already written; run
`uv run tarkov-tools sync` once that API is back and trader prices appear on
their own.

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

**Moving it.** The window has no system title bar, so there is a title strip
across the top that stands in for one: **anywhere along that strip drags the
window**. The search row and the bottom hint bar work too. Double-click any
of them to snap back to the centre.

The position is remembered in `config.local.json` and restored next time. It
is clamped to the virtual desktop on both drag and load, so it cannot be lost
off-screen - including on a multi-monitor setup where the second display sits
at negative coordinates. If a saved position is somehow unreachable it is
pulled back into view automatically.

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
- **Enter on a gun** → its attachment categories; arrow through them to see
  every part that fits, sorted by ergonomics then recoil.
- **Enter on a category** → those parts as rows of their own, so you can walk
  them one at a time, mark them, or open one on the wiki.
- **Enter on a part** → every weapon it fits, as rows. The detail pane only
  has room for the first handful, and a common part fits dozens.
- Enter on one of those weapons opens *its* categories, so you can follow a
  part onto a gun and straight into what else that gun takes.

**Esc backs out one level at a time**, however deep you went, and hides the
window once you are back at the results. Typing abandons the whole trail and
searches again.
- Search a **part** → its ergo/recoil/accuracy/loudness and every weapon it
  fits on, however many levels up that is.

### Your own lists

**Ctrl+H** marks something as in your stash, **Ctrl+D** as worth looking out
for. Marked items carry a star or diamond everywhere they appear - including
in a gun's parts list, so browsing a build shows at a glance which pieces you
already own. Each list gets its own Tab filter once it holds something.

### How many you have

**Ctrl+Shift+H** turns the search box into *"how many do you have?"* - type a
number, Enter saves it, `0` removes the item, Esc cancels and gives you your
search back. **Ctrl+Up / Ctrl+Down** nudge the count by one without leaving
the results, for when you pick up one more of something.

Counts show up next to the star everywhere - `★12 LEDX Skin Transilluminator`
- and the **Have filter lists everything you hold, most valuable pile first**.

From a terminal:

```powershell
uv run tarkov-tools stash              # everything, with totals
uv run tarkov-tools stash ledx 3       # set a count
uv run tarkov-tools stash ledx 0       # remove it
```

```
 qty         each         total  item
   3      583,680     1,751,040  LEDX Skin Transilluminator
   5       38,250       191,250  Salewa first aid kit

2 kinds, 8 items, 1,942,290 RUB at flea prices
```

Totals use the flea snapshot, and items banned from the flea count as zero
rather than being quietly dropped from the list.

The lists live in their own table, so re-importing templates or re-syncing an
account never disturbs them.
- Search an **extract** → side, chance, exfil timer, requirement and which
  spawns reach it. **Enter opens the interactive map on it.**

Results are tagged `GUN` / `AMO` / `MAG` / `EXT`, so extraction points share
the one search bar with everything else.

**Ctrl+Enter opens whatever is highlighted on the wiki** - a gun, a round, a
magazine, a part, any item at all. An extract opens its map's article, since
plain Enter already opens the interactive map on the exit itself.

**Ctrl+Shift+Enter opens its flea market page** on tarkovforge, straight to
the item rather than to a search box. Repeated presses reuse the one market
tab instead of stacking up a dozen.

**Type `:help`** in the search box for the full list of keys without leaving
the overlay.

The wiki does not always file an item under the name the game uses (the M4A1
lives at *Colt M4A1 5.56x45 assault rifle*), so the title is confirmed through
the wiki's API before the tab opens, and anything with no article at all lands
on a search instead of an empty page.

**Tab cycles a filter**, or jump straight to one with **Ctrl+<key>**. Keys run
`1 2 3 4 5 6 7 8 9 0` then `y u i o p`, the way browser tabs number themselves,
and each chip shows its own key. Shift+Tab steps back, and clicking a chip
works too.

**Ctrl+Shift+Left/Right** slides the active chip along the bar. The order is
saved to `config.local.json` and the keys follow the order, so whatever you
put first is always Ctrl+1.

The window sizes itself to fit the whole filter bar, growing as filters appear
and shrinking back when they go. Set `search.width` in `config.json` to pin it
to a fixed pixel width instead.

```
All | Guns | Ammo | Mags | Parts | Gear | Meds | Keys | Barter
    | Needed | Have | Watch | Extracts | Exfil PMC | Exfil Scav | Exfil Co-op
```

Every result carries a type tag - `GUN` `AMO` `MAG` `PRT` `GEAR` `MED` `KEY`
`BART` `FOOD` `NADE` `BOX` `CONT` `BLDE` `MAP` `CASH` `EXT` - because every
item is classified at import by its position in the game's category tree.

With the box empty, a filter lists that whole category - Tab to `Ammo` for the
full penetration chart, or `Exfil Scav` for all 62 Scav extracts. Sorted
sensibly per category: ammo by caliber then penetration, mags by capacity.

> Requires Tarkov in **borderless windowed** mode. Exclusive fullscreen will
> not composite another window on top.

---

## Quest and hideout needs (entirely optional)

**Skip this and everything else still works.** With no TarkovTracker account
the `Needed` filter is not offered at all, no quest data is fetched, and the
database never even gains the table. Gamma, guns, ammo, magazines and extracts
are unaffected.

Connect a [TarkovTracker](https://tarkovtracker.org/) account and every item
shows what still wants it:

```
Bundle of wires
  STILL NEEDED
     10x FIR  Fertilizers
     15x      Generator level 2
     10x      Heating level 3
```

```powershell
uv run tarkov-tools tracker login --token PVP_xxxxx   # or $env:TARKOVTRACKER_TOKEN
uv run tarkov-tools tracker sync                      # after playing
uv run tarkov-tools tracker needed                    # the shopping list
```

Tab to the **Needed** filter for everything outstanding, largest shortfall
first, with anything gated behind an unmet prerequisite marked `(locked)`.

**Press F5 in the popover** (or Ctrl+R) to re-sync without leaving the game.
It runs on a worker thread so the window stays usable, reports each stage as
it goes, and the `Needed` filter appears the moment a first sync completes.

**How it works.** The progress API returns only ids and completion flags - no
item names and no quantities remaining - so what you still need is computed by
joining your progress against TarkovTracker's task and hideout definitions,
which they serve publicly with no authentication. Those same public feeds are
a live substitute for the parts of tarkov.dev that are down.

Notes:

- **Read-only.** Only the `GP` permission is used; nothing writes to your
  account even if your token also carries `WP`.
- The token lives in `config.local.json`, which is gitignored, and is only
  ever displayed masked. `tracker logout` clears it.
- The prefix fixes the game mode: a `PVE_` token reads only PVE progress. A
  mismatch is a 401, which `tracker login` reports immediately.
- Currency is excluded - "you need 18,222,000 Roubles" is not a shopping list.
- Objectives that accept any of a wide list ("any of 56 medicine items") are
  kept off the shopping list but still shown on the item itself.
- Free tier allows 1,000 reads/day, so sync after a session rather than
  continuously.

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

## Sharing this with someone

A `git clone` (or `scripts\make-share-zip.cmd`, which uses `git archive`) is
safe: it carries tracked files only.

> **Do not zip the folder by hand.** That would include `config.local.json`,
> which holds your TarkovTracker token, and `data\`, which holds your
> database. Both are gitignored precisely so they never travel.

What the other person runs:

```powershell
uv sync
uv run tarkov-tools import-templates --download
uv run tarkov-tools
```

No account, no token, no API key. They get gamma, the search popover, the
full compatibility graph and the extract maps. If they later want quest
tracking they can add their own token; if they never do, nothing prompts them
about it.

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
