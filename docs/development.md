# Development notes

Background for working on tarkov-tools: where the data comes from, how the
compatibility graph is built, and why some things are the way they are. The
[README](../README.md) is the guide for using it.

---

## Layout

```
tarkov_tools/
  winapi.py       per-display gamma DCs, foreground process, monitor lookup
  gamma.py        ramp maths + focus watcher with guaranteed restore
  api.py          tarkov.dev GraphQL client, retry/backoff, response cache
  templates.py    raw BSG item-template importer (no API required)
  db.py           SQLite schema, upserts, FTS5 index
  ingest.py       API -> database, builds the compatibility graph
  prices.py       flea snapshot fetch and join
  market.py       flea market page slugs
  extracts.py     extraction points, wiki map URLs
  tracker.py      TarkovTracker progress -> what you still need
  search.py       search + relationship queries
  browser_tabs.py finds an already-open Chrome tab (the one comtypes user)
  hotkey.py       RegisterHotKey listener on its own message loop
  popover.py      tkinter search window
  cli.py          entry point ('start' runs gamma + popover in one process)
```

Almost entirely the standard library. The one dependency is `comtypes`,
used only to find an already-open Chrome tab; everything degrades gracefully
without it. Managed with [uv](https://docs.astral.sh/uv/).

---

## What the tool touches

Nothing reads, writes, or attaches to the game process.

| Component | How it works |
|---|---|
| Gamma | `SetDeviceGammaRamp` against a per-display device context. The same call the NVIDIA Control Panel slider makes. |
| Database | HTTP requests to a public API or a published template dump, cached into local SQLite. Read-only, and never during a raid. |
| Popover | An ordinary always-on-top window, like Notepad. No injection, no graphics-API hooking. |
| Hotkey | `RegisterHotKey`, which asks Windows to route one specific combination to us. Not a keyboard hook - it cannot see any other keystroke. |
| Focus | Reads the foreground window's title/class and executable *name* only, to know whether the game is in front. |

There is no DLL injection, no `Present`/`vkQueuePresentKHR` hooking, and no
handle opened against the game beyond `PROCESS_QUERY_LIMITED_INFORMATION` to
read an executable name for focus detection.

---

## Where the data comes from

Two kinds of data, with very different sources and lifetimes:

- **Static stats** (penetration, damage, ergonomics, magazine capacity, and
  crucially the compatibility lists) originate in the game's own item
  templates. They are the game's real numbers, not community measurements,
  and they only change when the game patches.
- **Flea prices** come from tarkov.dev's closed-source scanners, which page
  through live market listings. This is the only part that needs a live feed.

### The compatibility graph

Where each edge comes from in the raw template format:

```
weapon._props.Chambers[]._props.filters[].Filter          -> weapon accepts ammo
weapon._props.Slots[_name=mod_magazine].filters[].Filter  -> weapon accepts magazine
magazine._props.Cartridges[]._props.filters[].Filter      -> magazine accepts ammo
```

All three come straight from the item templates, so "what fits what" is
authoritative rather than inferred. Items are classified by walking the
`_parent` chain to a root category (Weapon `5422acb9...`, Ammo `5485a868...`,
Magazine `5448bc23...`), which picks up every subcategory automatically.

Templates only carry internal names such as `patron_556x45_M995`; display
names live in a separate locale file keyed `"<id> Name"`, which is why the
importer wants both files.

---

## Building the database

Two independent ways. Neither needs an API key. The first run of the app does
option A automatically (`cli.ensure_database`), so these are for rebuilding.

### Option A - raw game templates (default, no API)

```powershell
uv run tarkov-tools import-templates --download
```

Downloads a dump of the game's own item template database (~18 MB) plus the
English locale file (~2.9 MB) and imports in about a second. It carries **no
market prices** - those do not exist in the game files.

### Option B - the tarkov.dev API

```powershell
uv run tarkov-tools sync
```

Same static data, plus flea prices and trader offers.

> **`GraphQL server unavailable` / HTTP 422** is an outage on their side, not
> an auth problem - their Cloudflare Worker cannot reach its data backend.
> Auth failures would be 401/403. Retry later; the client already backs off
> and retries five times.

The two are designed to coexist. A template import never touches the price
columns, so static data can be refreshed offline and prices layered on top
with `sync` whenever the API is reachable.

### Changing the template source

Any dump in the raw BSG format works. Point `config.json` somewhere else if
the default mirror goes stale:

```jsonc
"templates": {
  "items_url":  "https://.../database/templates/items.json",
  "locale_url": "https://.../database/locales/global/en.json"
}
```

Or import files already on disk:

```powershell
uv run tarkov-tools import-templates --items path\to\items.json --locale path\to\en.json
```

SPT (Single Player Tarkov) maintains the canonical dump, but at time of
writing its GitHub mirror keeps `items.json` behind git-lfs with an exhausted
LFS budget, and its own Gitea returns 410 - hence the plain-blob mirror in the
default config.

---

## Prices

tarkov.dev's GraphQL API is the community's canonical price source and has
been returning *"GraphQL server unavailable"*. So prices come from
[tarkovforge](https://tarkovforge.com/market)'s public snapshot instead: one
static JSON file, refreshed hourly, keyed by the same BSG item ids this
database already uses, so it joins straight on with no name matching. It is
derived from tarkov.dev, so it is the same data one step removed.

### Ammo is priced by the box

The snapshot has **no entry for a single round** - 0 of 208 - because the flea
does not trade them. It trades ammo packs, and covers 82% of those. So a
round's price is worked back from the cheapest pack it is sold in. The pack
contents come from the item templates (`StackSlots[0]` names the round and
holds a `_max_count`), which links 213 boxes and gives **143 of 208 rounds a
price**. The rest - M995, SSA AP and friends - genuinely cannot be traded.

### When an item has no market page

Of 4,613 items, 3,379 have a market page and the rest split two ways, which is
why Ctrl+Shift+Enter behaves differently depending on which:

| | market page | flea price | what happens |
|---|---|---|---|
| 3,379 items | yes | either | opens the item's page |
| 351 items | no | yes | opens the market search - it *is* tradeable, their site just has no page |
| 883 items | no | no | says **"not traded on the flea market"** and stays put |

That last case is quest items, ammo boxes and intel. Opening a market search
for them would show an empty table, so the overlay explains why instead of
hiding itself behind a dead end.

### Not there yet: which trader pays the most

That needs per-trader sell prices, which only tarkov.dev has - tarkovforge
reads them live from the same API rather than publishing them, and the raw
item templates carry no prices at all. The display for it is already written;
run `uv run tarkov-tools sync` once that API is back and trader prices appear
on their own.

---

## TarkovTracker

The progress API returns only ids and completion flags - no item names and no
quantities remaining - so what you still need is computed by joining your
progress against TarkovTracker's task and hideout definitions, which they
serve publicly with no authentication. Those same public feeds are a live
substitute for the parts of tarkov.dev that are down.

- **Read-only.** Only the `GP` permission is used; nothing writes to the
  account even if the token also carries `WP`.
- The token lives in `config.local.json` (gitignored) and is only ever
  displayed masked. `tracker logout` clears it.
- The prefix fixes the game mode: a `PVE_` token reads only PVE progress. A
  mismatch is a 401, which `tracker login` reports immediately.
- Currency is excluded - "you need 18,222,000 Roubles" is not a shopping list.
- Objectives that accept any of a wide list ("any of 56 medicine items") are
  kept off the shopping list but still shown on the item itself.
- Free tier allows 1,000 reads/day, so sync after a session rather than
  continuously.

---

## Personal lists

`have` and `watch` live in one `stash` table keyed by item id, so re-importing
templates or re-syncing an account never disturbs them.

Rows whose `item_id` is not a real item id are junk: the result list also
carries `extract:<n>` and `recent:<term>` pseudo-rows, and marking one used to
write a stash row that no join could resolve, leaving a Have chip in the
filter bar with an empty list behind it. `Popover._selected_item_id` rejects
both prefixes. To clean up a database that predates that fix:

```sql
DELETE FROM stash WHERE item_id NOT IN (SELECT id FROM items);
```

---

## Demo data

`scripts/make_demo_data.py` writes a small synthetic dataset in the exact
shape the API returns, so the code can be exercised while tarkov.dev is down.

**Every demo item is named `[DEMO] ...` and every stat in it is invented.**
Run a real `sync` to replace it with authoritative values.

```powershell
uv run python scripts\make_demo_data.py
uv run tarkov-tools sync --cache
```

---

## Not built yet

The extract-map overlay: read the map name from the game's own log files
(`%LOCALAPPDATA%\Battlestate Games\EscapeFromTarkov\Logs`), OCR the extract
panel, fuzzy-match against the known extract list for that map, and draw the
tarkov.dev SVG with your extracts highlighted. The map SVGs and the
world-to-pixel transforms are already published.

---

## Parked investigations

- [Chrome "Aw, Snap!" when opening wiki pages](chrome-crashes.md)
