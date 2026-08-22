# tarkov-tools

Two things for Escape from Tarkov, in one small Windows app:

- **A search overlay.** Press `Ctrl+T` mid-raid and ask what ammo a gun takes,
  what a round costs, which magazines fit, or where an extract is - without
  alt-tabbing to the wiki.
- **Automatic gamma.** Your brightness setting applies while Tarkov has focus,
  and only on the monitor the game is on. Alt-tab to Discord and your desktop
  looks normal again. Arena counts as the game too.

```
 search  m855a1

 AMO  5.56x45mm M855A1        5.56x45mm M855A1
 BOX  5.56x45mm ammo pack       flea 3,077 RUB/round   153,848 for 50
 BOX  5.56x45mm ammo pack
                                pen 44   dmg 49   armor dmg 47%   frag 44%
                                Caliber556x45NATO   945.0 m/s

                                FIRED BY
                                  ADAR 2-15 5.56x45 carbine    ergo 48  rec 120
                                  Colt M4A1 5.56x45            ergo 48  rec 119
                                  HK 416A5 5.56x45             ergo 51  rec 125
```

Everything runs locally. There is no account to make and no API key to get.

---

## Getting started

You need **Windows**, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and **Chrome** if you want the wiki and map features. Python itself comes from
uv, so there is nothing else to install.

```powershell
git clone git@github.com:natelwatts/tarkov-tools.git
cd tarkov-tools
uv sync
uv run tarkov-tools
```

The first run downloads the game's item data and builds a local database.
That takes a minute and happens once; every run after it starts straight
away. After that you have both tools running: press `Ctrl+T` any time to
search, and gamma follows the game.

Ctrl-C in the terminal, or `:q!` in the overlay, stops both and restores your
gamma. `scripts\tarkov-tools.cmd` starts it by double-click instead.

> Tarkov must be in **borderless windowed** mode for the overlay to appear on
> top of it. Exclusive fullscreen will not composite another window over the
> game.

---

## Using the overlay

Press `Ctrl+T` and start typing. The box always has the keyboard - there is no
mode to be in, and no command to learn first.

- Search a **gun** and you get every round it fires sorted by penetration,
  every magazine that fits sorted by capacity, and which traders sell it.
- Search a **round** and you get every gun that fires it and every magazine
  that holds it.
- Search a **part** and you get its ergonomics, recoil, accuracy and loudness,
  plus every weapon it goes on.
- Search an **extract** and you get its side, chance, timer, requirement and
  which spawns reach it.

Every result is tagged `GUN` `AMO` `MAG` `PRT` `GEAR` `MED` `KEY` `EXT` and so
on, so extraction points share the one search bar with everything else.

### The keys

| key | does |
|---|---|
| `Up` `Down` | next row, previous row |
| `Ctrl+j` `Ctrl+k` | the same, without leaving the home row |
| `Enter` | open what's highlighted |
| `Esc` / `Backspace` | back a level, or clear the search box at the top |
| `Tab` / `Left` `Right` | switch filter |
| `Ctrl+h` `Ctrl+l` | switch filter, whatever the arrows are set to |
| `Ctrl+1`-`0`, `y`-`p` | jump straight to a filter |
| `Ctrl+Enter` | open it on the wiki |
| `Ctrl+Shift+Enter` | open its flea market page |
| `F5` | refresh prices, and quest progress if connected |
| `:help` | every key, without leaving the overlay |
| `:allergy` | allergies you want kept to hand |
| `:q` | close the window, everything keeps running |
| `:q!` | quit properly - stops the gamma watcher and restores gamma |

`Ctrl` + `hjkl` moves as well as the arrows do: `j`/`k` down and up the rows,
`h`/`l` left and right along the filters. It is not a mode - nothing is
switched on, and the box is still taking letters.

Rows **wrap**: `Up` from the top row takes you to the bottom, and `Down` from
the bottom comes back to the top, the same way the filter chips do.

**Nothing but `:q` closes the window.** `Esc` at the top level clears whatever
you typed; press it again on an empty box and it says so and stays put, rather
than dismissing what you were reading. Your hotkey still toggles it.

The line along the bottom shows only the keys that apply to where you are.

### Going deeper with Enter

`Enter` opens whatever is highlighted, and you can keep going:

- **On a gun** - Ammo first, then its attachment categories. Ammo leads
  because "what do I feed it, and what does that cost" is the question a gun
  raises in a raid; which foregrip fits is not.
- **On Ammo** - every round it fires, hardest-hitting first, each priced.
- **On a category** - the parts that fit, best ergonomics first.
- **On a part** - every weapon it goes on. Enter one of those and you are in
  *its* categories, so you can follow a part onto a gun and straight into what
  else that gun takes.
- **On an extract** - the interactive wiki map, zoomed out, with that exit
  marked.

`Esc` backs out one level at a time, however deep you went. Typing abandons
the trail and searches again.

### Filters

`Tab` cycles the filter chips along the top, or jump straight to one with
`Ctrl` + the key printed on the chip. With the box empty, a filter lists that
whole category - `Tab` to `Ammo` for the full penetration chart, or
`Exfil Scav` for every Scav extract.

```
All | Guns | Ammo | Mags | Parts | Gear | Meds | Keys | Barter
    | Needed | Have | Watch | Extracts | Exfil PMC | Exfil Scav | Exfil Co-op
```

`Ctrl+Shift+Left/Right` slides the active chip along the bar, and the order is
remembered - so whatever you put first is always `Ctrl+1`.

Leave the box empty on **All** and it lists what you looked up recently.
`Enter` runs one again, `Ctrl+Del` forgets it.

### Moving the window

The overlay has no title bar, so the strip across the top stands in for one:
anywhere along it drags the window, and so do the search row and the bottom
hint bar. Double-click any of them to snap back to the centre. Where you put
it is remembered.

---

## Prices

Search anything and the detail pane leads with what it sells for, what that
works out to **per inventory slot**, and which way the price has moved:

```
Salewa first aid kit
  flea 38,893 RUB   19,446/slot (2 slots)   +4%
```

Per slot is the number that decides what comes home - a 200k item filling six
slots loses to a 90k item filling one. It is deliberately left off weapons: a
built gun's footprint depends on what is bolted to it.

Ammo is priced **by the box**, because that is how the flea trades it:

```
5.56x45mm M855A1
  flea 3,077 RUB/round   153,848 for 50
```

Prices refresh by themselves when the overlay starts, and whenever you press
`F5`, so what you see mid-raid is current to the hour without touching a
terminal.

**An item with no price is not missing** - it is banned from the flea market,
and the tool says so rather than showing a blank. M995 and friends genuinely
cannot be traded.

---

## Your stash and watch lists

Mark things you own, and things you want to pick up. Both lists are yours
alone and live in the local database.

| key | does |
|---|---|
| `Ctrl+S` | put the highlighted item on your **Have** list, or take it off |
| `Ctrl+Shift+S` | say how many you have - `Enter` saves, `0` removes |
| `Ctrl+Up` `Ctrl+Down` | that count, one up or one down |
| `Ctrl+Del` | straight off the list, whatever the count |
| `Ctrl+D` | put it on your **Watch** list, or take it off |

Marked items carry a star (`★`) or a diamond (`◆`) everywhere they appear,
including inside a gun's parts list - so browsing a build shows at a glance
which pieces you already own. Counts show up beside the star:
`★12 LEDX Skin Transilluminator`.

**To see a whole list**, `Tab` round to its chip. A **Have** chip appears in
the filter bar as soon as you hold something, and lists everything you own,
most valuable pile first, with a running total:

```
LEDX Skin Transilluminator
  ★ in your stash x4
    Ctrl+Shift+S change count   Ctrl+Up/Down +1/-1   Ctrl+Del remove
```

The same from a terminal:

```powershell
uv run tarkov-tools stash              # everything, with totals
uv run tarkov-tools stash ledx 3       # set a count
uv run tarkov-tools stash ledx 0       # remove it
uv run tarkov-tools stash --clear      # empty the list
```

```
 qty         each         total  item
   3      573,044     1,719,132  LEDX Skin Transilluminator
   5       38,893       194,465  Salewa first aid kit

2 kinds, 8 items, 1,913,597 RUB at flea prices
```

---

## Allergies

Nothing to do with the game - the overlay is the thing already bound to a
hotkey, so it is a good place to keep something you need to be able to recall
on the spot.

```
:allergy                 what is saved
:allergy peanuts         save one - Enter confirms
:allergy rm peanuts      take it off again
```

Notes are kept exactly as you type them, matched without regard to case, and
stored in the same local database as everything else. Multi-word notes are
fine: `:allergy shellfish (mild)`.

---

## The wiki and the map

`Ctrl+Enter` opens whatever is highlighted **on the wiki** - a gun, a round, a
part, any item at all. The wiki does not always file an item under the name
the game uses (the M4A1 lives at *Colt M4A1 5.56x45 assault rifle*), so the
title is confirmed before the tab opens, and anything with no article lands on
a search rather than an empty page.

`Ctrl+Shift+Enter` opens its **flea market page**, straight to the item.
Repeated presses reuse the one market tab instead of stacking up a dozen.

`Enter` on an extract opens the **interactive map** with that exit marked and
the view pulled back so you can see where it actually is. From a terminal:

```powershell
uv run tarkov-tools extract zb-1011
```

---

## Quest and hideout needs (optional)

**Skip this and everything else still works.** With no
[TarkovTracker](https://tarkovtracker.org/) account the `Needed` filter is not
offered at all and no quest data is ever fetched.

Connect one and every item shows what still wants it:

```
Bundle of wires
  flea 26,756 RUB   26,756/slot   -7%

  STILL NEEDED
     10x FIR  Fertilizers
      2x      Defective Wall level 6
     15x      Generator level 2
     10x      Heating level 3
```

```powershell
uv run tarkov-tools tracker login --token PVP_xxxxx
uv run tarkov-tools tracker sync                      # after playing
uv run tarkov-tools tracker needed                    # the shopping list
```

`Tab` to the **Needed** filter for everything outstanding, largest shortfall
first, with anything gated behind an unmet prerequisite marked `(locked)`.
`F5` in the overlay re-syncs without leaving the game.

The token is read-only, stored locally, and never displayed unmasked.
`tracker logout` clears it.

---

## Gamma

Your chosen gamma applies **only while Tarkov has focus** - Escape from
Tarkov or Escape from Tarkov: Arena, both watched by default - and only on the
monitor the game window is on. The original setting is always restored on the
way out, so a crash cannot leave your desktop washed out.

Running `uv run tarkov-tools` starts this alongside the overlay. On its own:

```powershell
uv run tarkov-tools gamma watch      # the one you want running
uv run tarkov-tools gamma set 1.5    # apply right now
uv run tarkov-tools gamma reset      # back to neutral
uv run tarkov-tools gamma displays   # list monitors + current state
```

Change the default in `config.json` (`"gamma": { "value": 1.5 }`), or pass one
for a single run with `uv run tarkov-tools start 1.6`.

Summoning the overlay does not count as leaving the game - gamma stays applied
and stays on the game's monitor.

---

## All the commands

`tt` is a shorter alias for `tarkov-tools`, and **every command explains
itself** with `--help`.

| Command | What it does |
|---|---|
| `uv run tarkov-tools` | gamma watcher + overlay together |
| `uv run tarkov-tools start 1.6` | same, overriding the gamma value |
| `uv run tarkov-tools popover` | overlay only |
| `uv run tarkov-tools gamma watch` | gamma only |
| `uv run tarkov-tools search m995` | one-off lookup in the terminal |
| `uv run tarkov-tools ammo` | penetration chart, all calibers |
| `uv run tarkov-tools stash` | what you have and what it is worth |
| `uv run tarkov-tools prices update` | pull the latest flea snapshot |
| `uv run tarkov-tools extract zb-1011` | open the map on that extract |
| `uv run tarkov-tools tracker sync` | refresh quest progress |
| `uv run tarkov-tools hotkey ctrl+alt+k` | rebind the overlay hotkey |
| `uv run tarkov-tools import-templates --download` | rebuild the item database |

---

## Settings

`config.json` is written on first run. Anything machine-specific - your
hotkey, window position, token - goes in `config.local.json`, which is never
committed.

```jsonc
{
  "gamma":  { "value": 1.5, "game_monitor_only": true },
  "search": { "hotkey": "ctrl+t",
              "arrow_keys_switch_filters": "always" }
}
```

Useful ones:

| setting | what it does |
|---|---|
| `gamma.value` | how bright, while the game has focus |
| `gamma.exes` | which executables count as the game (both Tarkov and Arena by default) |
| `gamma.game_monitor_only` | leave your other monitors alone |
| `search.hotkey` | what summons the overlay |
| `search.arrow_keys_switch_filters` | `always`, `edges` (only when the caret can't move), or `never` |
| `search.width` | pin the overlay to a fixed width instead of fitting the filter bar |

### Rebinding the hotkey

```powershell
uv run tarkov-tools hotkey                  # show the current binding
uv run tarkov-tools hotkey ctrl+alt+k       # change it
```

Accepted forms: `ctrl+t`, `f9`, `ctrl+shift+space`, `win+k`. Modifiers can be
side-specific - `rctrl+t` fires only on the right Ctrl. The new combination is
registered for real before it is saved, so a clash is reported immediately
rather than failing silently next time.

> A hotkey is claimed system-wide, so while the overlay runs **that
> combination stops reaching other applications**. `Ctrl+T` is "new tab" in
> every browser, so expect that to stop working until you exit. `ctrl+alt+t`
> or `ctrl+shift+space` are far less contested.

---

## If something goes wrong

**The overlay doesn't appear over the game.** Tarkov has to be in borderless
windowed mode. Exclusive fullscreen cannot composite another window on top.

**`Ctrl+T` does nothing.** Something else has claimed it. Pick another with
`uv run tarkov-tools hotkey ctrl+alt+k`.

**Searches find nothing at all.** The item database is empty - rebuild it with
`uv run tarkov-tools import-templates --download`.

**Prices are missing or stale.** `uv run tarkov-tools prices update`, or `F5`
in the overlay. Items banned from the flea have no price by definition.

**Gamma won't go far enough.** Windows limits how far a gamma ramp may deviate
from linear. Run this once from an Administrator shell, then sign out and back
in:

```powershell
uv run tarkov-tools gamma --unlock-range
```

---

## Is this safe to run?

Nothing here reads, writes, or attaches to the game process. The gamma control
is the same Windows call the NVIDIA Control Panel slider makes; the overlay is
an ordinary always-on-top window, like Notepad; the hotkey is registered with
Windows rather than hooked, so it cannot see any other keystroke. Nothing is
injected, and no game files or memory are touched.

That said: BSG has never affirmatively blessed this category of tool. Their
only official statement prohibits software that replaces, overrides, or
modifies game files or memory - none of which this does - but "not prohibited"
is not the same as "approved". Use your own judgement.

[The development notes](docs/development.md) spell out exactly which Windows
calls are involved.

---

## Sharing it with someone

A `git clone`, or `scripts\make-share-zip.cmd`, carries tracked files only.

> **Do not zip the folder by hand.** That would include `config.local.json`,
> which holds your TarkovTracker token, and `data\`, which holds your database
> and your stash list. Both are gitignored precisely so they never travel.

What the other person runs:

```powershell
uv sync
uv run tarkov-tools
```

No account, no token, no API key. They get gamma, the search overlay, the full
compatibility graph and the extract maps.

---

Working on the code? See [docs/development.md](docs/development.md) for where
the data comes from, how the compatibility graph is built, and the module
layout.
