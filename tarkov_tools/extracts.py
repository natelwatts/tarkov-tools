"""Extraction points: look one up and open the map focused on it.

Extract data comes from the game's own location files (allExtracts.json per
map), so names, side, chance, timers and requirements are authoritative and
available offline.

Focusing the extract needs no extension and no injected JavaScript. The
wiki's interactive map pages accept a marker id:

    https://escapefromtarkov.fandom.com/wiki/Map:Customs?marker=53

The map bundle reads it with URLSearchParams and opens zoomed in on that
marker with its popup showing. Marker ids are scraped from the Map: page,
which embeds the full marker list as JSON, and cached in the database.

Chrome's scroll-to-text-fragment (#:~:text=) was tried first and does NOT
work here: on the article pages the extract names appear only inside the
map's embedded JSON, and text fragments match rendered text only.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import db as dbmod
from .config import DATA_DIR

WIKI_BASE = "https://escapefromtarkov.fandom.com/wiki/"
RAW_BASE = (
    "https://raw.githubusercontent.com/paulov-t/Paulov.Tarkov.Db/master/database"
)
USER_AGENT = "tarkov-tools/0.1 (personal, non-commercial)"
EXTRACT_DIR = DATA_DIR / "templates" / "locations"

# SPT location folder -> (display name, wiki page).
# The folder names are internal and several do not match either the in-game
# name or the wiki title, so the mapping is explicit rather than derived.
LOCATIONS: dict[str, tuple[str, str]] = {
    "bigmap": ("Customs", "Customs"),
    "factory4_day": ("Factory (Day)", "Factory"),
    "factory4_night": ("Factory (Night)", "Factory"),
    "interchange": ("Interchange", "Interchange"),
    "laboratory": ("The Lab", "The_Lab"),
    "labyrinth": ("Labyrinth", "The_Labyrinth"),
    "lighthouse": ("Lighthouse", "Lighthouse"),
    "rezervbase": ("Reserve", "Reserve"),
    "sandbox": ("Ground Zero", "Ground_Zero"),
    "sandbox_high": ("Ground Zero (high)", "Ground_Zero"),
    "shoreline": ("Shoreline", "Shoreline"),
    "tarkovstreets": ("Streets of Tarkov", "Streets_of_Tarkov"),
    "terminal": ("Terminal", "Terminal"),
    "woods": ("Woods", "Woods"),
}

# Maps the template mirror has no allExtracts.json for. Their extracts are
# taken from the wiki's own map markers instead, which gives the name, side
# and marker link but none of the game-file detail (chance, timers, rules).
WIKI_ONLY_MAPS: dict[str, tuple[str, str]] = {
    "icebreaker": ("Icebreaker", "Icebreaker"),
    "terminal": ("Terminal", "Terminal"),
}

# Wiki marker categories -> the game's Side values.
CATEGORY_SIDES = {
    "exfil_pmc": "Pmc",
    "exfil_scav": "Scav",
    "exfil_shared": "Coop",
    "exfil_coop": "Coop",
    "transit": "Transit",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS extracts (
    map_id          TEXT NOT NULL,
    map_name        TEXT,
    wiki_page       TEXT,
    name            TEXT NOT NULL,
    display_name    TEXT,
    side            TEXT,
    chance          REAL,
    exfil_time      INTEGER,
    min_time        INTEGER,
    max_time        INTEGER,
    requirement     TEXT,
    requirement_tip TEXT,
    entry_points    TEXT,
    marker_id       TEXT,
    search_key      TEXT,
    PRIMARY KEY (map_id, name, side)
);
CREATE INDEX IF NOT EXISTS idx_extracts_name ON extracts(display_name);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    # marker_id was added after the first release; keep older databases usable.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(extracts)")}
    if "marker_id" not in columns:
        conn.execute("ALTER TABLE extracts ADD COLUMN marker_id TEXT")
    if "search_key" not in columns:
        conn.execute("ALTER TABLE extracts ADD COLUMN search_key TEXT")


def _match_key(name: str) -> str:
    """Loose key for matching wiki marker titles to game extract names."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


# --- import ------------------------------------------------------------

def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def download(dest: Path | None = None, verbose: bool = True) -> Path:
    """Fetch allExtracts.json for every known map."""
    dest = dest or EXTRACT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    for location in LOCATIONS:
        target = dest / f"{location}.json"
        try:
            data = _fetch(f"{RAW_BASE}/locations/{location}/allExtracts.json")
        except Exception as exc:
            if verbose:
                print(f"  {location:16} skipped ({exc})")
            continue
        target.write_bytes(data)
        if verbose:
            count = len(json.loads(data.decode("utf-8")))
            print(f"  {location:16} {count:>3} extracts")
    return dest


def fetch_all_markers(verbose: bool = True) -> dict[str, dict[str, str]]:
    """{wiki_page: {match_key: marker_id}} for every map, fetched once each.

    Requests are spaced out and retried; hammering the wiki earns a 403.
    """
    out: dict[str, dict[str, str]] = {}
    pages = sorted({page for _, page in LOCATIONS.values()})
    for page in pages:
        for attempt in range(3):
            try:
                markers = fetch_map_markers(page)
                out[page] = {_match_key(t): i for t, i in markers.items()}
                if verbose:
                    print(f"  {page:22} {len(markers):>3} map markers")
                break
            except Exception as exc:
                if attempt == 2:
                    if verbose:
                        print(f"  {page:22} no markers ({exc})")
                else:
                    time.sleep(5.0 * (attempt + 1))
        time.sleep(1.5)
    return out


def import_extracts(conn, source: Path | None = None, locale: dict | None = None,
                    markers: dict[str, dict[str, str]] | None = None,
                    verbose: bool = True) -> int:
    """Load downloaded extract files into the database."""
    source = source or EXTRACT_DIR
    ensure_schema(conn)
    locale = locale or {}
    markers = markers or {}
    total = 0
    matched = 0

    for location, (map_name, wiki_page) in LOCATIONS.items():
        path = source / f"{location}.json"
        if not path.exists():
            continue
        page_markers = markers.get(wiki_page, {})
        entries = json.loads(path.read_text(encoding="utf-8"))
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            raw = entry.get("Name")
            if not raw:
                continue
            # Several names are internal (EXFIL_ZB013, customs_sniper_exit);
            # the locale file maps them to what the game actually shows.
            display = locale.get(raw) or raw
            # Match the game's name to the wiki marker. Try the display name,
            # then the raw name, then the name without a parenthetical suffix.
            marker_id = (
                page_markers.get(_match_key(display))
                or page_markers.get(_match_key(raw))
                or page_markers.get(_match_key(display.split(" (")[0]))
            )
            if marker_id:
                matched += 1
            conn.execute(
                """
                INSERT INTO extracts (map_id, map_name, wiki_page, name, display_name,
                    side, chance, exfil_time, min_time, max_time,
                    requirement, requirement_tip, entry_points, marker_id, search_key)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(map_id, name, side) DO UPDATE SET
                    map_name=excluded.map_name, wiki_page=excluded.wiki_page,
                    display_name=excluded.display_name, chance=excluded.chance,
                    exfil_time=excluded.exfil_time, min_time=excluded.min_time,
                    max_time=excluded.max_time, requirement=excluded.requirement,
                    requirement_tip=excluded.requirement_tip,
                    entry_points=excluded.entry_points,
                    marker_id=COALESCE(excluded.marker_id, extracts.marker_id),
                    search_key=excluded.search_key
                """,
                (
                    location, map_name, wiki_page, raw, display,
                    entry.get("Side"), entry.get("Chance"),
                    entry.get("ExfiltrationTime"), entry.get("MinTime"),
                    entry.get("MaxTime"), entry.get("PassageRequirement"),
                    locale.get(entry.get("RequirementTip") or "", entry.get("RequirementTip")),
                    entry.get("EntryPoints"), marker_id,
                    _match_key(display) + "|" + _match_key(raw) + "|" + _match_key(map_name),
                ),
            )
            total += 1
    if verbose:
        print(f"  imported {total} extracts across {len(LOCATIONS)} maps"
              f"  ({matched} linked to a map marker)")
    return total


# --- lookup ------------------------------------------------------------

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _parse_markers(html: str) -> list[dict]:
    """Pull the interactive map's marker array out of a Map: page.

    The page embeds the whole map definition as plain JSON under a "markers"
    key, so the array is located and bracket-matched rather than regexed,
    which keeps nested objects and escaped quotes intact.
    """
    key = '"markers":['
    start = html.find(key)
    if start == -1:
        return []
    start += len(key) - 1  # position of the opening bracket
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(html)):
        char = html[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:index + 1])
                except json.JSONDecodeError:
                    return []
    return []


def fetch_marker_records(wiki_page: str) -> list[dict]:
    """[{title, id, category}] for a Map: page's extraction/transit markers.

    Only exfil and transit categories are kept, so a loot or spawn marker
    cannot shadow an extract that happens to share its name.
    """
    url = f"{WIKI_BASE}Map:{wiki_page}"
    request = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(request, timeout=60) as response:
        html = response.read().decode("utf-8", "replace")

    out: list[dict] = []
    for marker in _parse_markers(html):
        category = str(marker.get("categoryId") or "")
        if not category.startswith(("exfil", "transit")):
            continue
        title = ((marker.get("popup") or {}).get("title") or "").strip()
        marker_id = marker.get("id")
        if title and marker_id is not None:
            out.append({"title": title, "id": str(marker_id), "category": category})
    return out


def fetch_map_markers(wiki_page: str) -> dict[str, str]:
    """{marker title: marker id} - the id is what ?marker= takes."""
    out: dict[str, str] = {}
    for record in fetch_marker_records(wiki_page):
        out.setdefault(record["title"], record["id"])
    return out


def import_wiki_only_maps(conn, verbose: bool = True) -> int:
    """Add extracts for maps the template mirror does not cover.

    These carry name, side and marker link only - the game-file detail simply
    is not available for them from this source.
    """
    ensure_schema(conn)
    total = 0
    for map_id, (map_name, wiki_page) in WIKI_ONLY_MAPS.items():
        existing = conn.execute(
            "SELECT COUNT(*) FROM extracts WHERE map_id = ?", (map_id,)
        ).fetchone()[0]
        if existing:
            continue  # real game data already imported for this map
        try:
            records = fetch_marker_records(wiki_page)
        except Exception as exc:
            if verbose:
                print(f"  {map_name:20} skipped ({exc})")
            continue
        for record in records:
            side = CATEGORY_SIDES.get(record["category"], "Pmc")
            name = record["title"]
            conn.execute(
                """
                INSERT INTO extracts (map_id, map_name, wiki_page, name, display_name,
                    side, marker_id, search_key)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(map_id, name, side) DO UPDATE SET
                    marker_id=excluded.marker_id, search_key=excluded.search_key
                """,
                (
                    map_id, map_name, wiki_page, name, name, side, record["id"],
                    _match_key(name) + "|" + _match_key(name) + "|" + _match_key(map_name),
                ),
            )
            total += 1
        if verbose:
            print(f"  {map_name:20} {len(records):>3} extracts (from wiki markers)")
        time.sleep(1.5)
    return total


def search(conn, term: str, limit: int = 25, side: str | None = None) -> list[dict]:
    """Find extracts by display or internal name, best matches first."""
    ensure_schema(conn)
    term = (term or "").strip()
    if not term:
        return []
    like = f"%{term}%"
    # Normalised key so punctuation never blocks a match: "smugglers boat"
    # finds "Smugglers' Boat", and "d-2" finds "EXFIL_Bunker_D2".
    keyed = f"%{_match_key(term)}%"
    side_clause = " AND side = ?" if side else ""
    rows = conn.execute(
        f"""
        SELECT rowid, * FROM extracts
        WHERE (display_name LIKE ? OR name LIKE ? OR map_name LIKE ?
           OR search_key LIKE ?){side_clause}
        ORDER BY
            CASE WHEN LOWER(display_name) = LOWER(?) THEN 0
                 WHEN display_name LIKE ? THEN 1 ELSE 2 END,
            LENGTH(display_name), map_name
        LIMIT ?
        """,
        (like, like, like, keyed, *( (side,) if side else () ), term, f"{term}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


# The game stores a requirement code plus a locale tip that often still has
# an unfilled placeholder ("Bring {0}"), so the codes are given plain wording.
REQUIREMENT_LABELS = {
    "TransferItem": "paid extract - costs roubles or an item",
    "ScavCooperation": "co-op with a Scav",
    "WorldEvent": "needs a lever or switch activated",
    "Train": "train - wait for departure",
    "Reference": "needs an item",
    "None": None,
    "Empty": None,
    "": None,
}


def requirement_text(extract: dict) -> str | None:
    """Human wording for an extract's requirement, or None if it has none."""
    code = (extract.get("requirement") or "").strip()
    if code in REQUIREMENT_LABELS:
        label = REQUIREMENT_LABELS[code]
    else:
        label = code or None
    tip = (extract.get("requirement_tip") or "").strip()
    # Drop tips that are not real prose: unsubstituted templates ("Bring {0}")
    # and raw locale keys that had no translation ("EXFIL_Cooperate").
    looks_like_key = bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(_[A-Za-z0-9]+)+", tip))
    # BSG leaves developer notes in some tips, e.g. "TIP IS HARDCODED".
    looks_like_note = tip.isupper() and len(tip) > 3
    if (tip and "{" not in tip and not looks_like_key and not looks_like_note
            and tip.lower() != code.lower()):
        return f"{label} ({tip})" if label else tip
    return label


def wiki_url(extract: dict) -> str:
    """URL that opens the map focused on this extract.

    The interactive map page (Map: namespace) takes a ?marker=<id> parameter -
    its bundle reads it via URLSearchParams - and opens zoomed in on that
    marker with its popup showing. The app then strips the parameter from the
    address bar, which is expected and not a sign that it was ignored.

    A handful of extracts have no locale entry, so their name is an internal
    one that matches no marker title. Those still open the right interactive
    map - just not focused - which is more useful than the article page,
    since the map has its own search box.
    """
    page = extract.get("wiki_page") or "Escape_from_Tarkov_Wiki"
    marker_id = extract.get("marker_id")
    if marker_id:
        return f"{WIKI_BASE}Map:{page}?marker={urllib.parse.quote(str(marker_id))}"
    return f"{WIKI_BASE}Map:{page}"


def apply_map_filters(wiki_page: str, wanted=None, verbose: bool = False) -> bool:
    """Hide every map category except the ones worth seeing.

    Best effort: the map exposes no filter URL parameter, so this drives its
    sidebar. Any failure just leaves the map unfiltered.
    """
    try:
        from .browser_tabs import _window_text, chrome_windows
        from .map_filters import DEFAULT_WANTED, apply_filters

        wanted = tuple(tuple(pair) for pair in (wanted or DEFAULT_WANTED))
        needle = f"Map:{wiki_page}".lower()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            for hwnd in chrome_windows():
                if needle in (_window_text(hwnd) or "").lower():
                    return apply_filters(hwnd, wanted=wanted, verbose=verbose)
            time.sleep(0.6)
    except Exception as exc:
        if verbose:
            print(f"  map filters skipped: {exc}")
    return False


def find_chrome() -> str | None:
    import shutil
    import winreg

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
            with winreg.OpenKey(hive, key_path) as key:
                path = winreg.QueryValueEx(key, "")[0]
                if path and Path(path).exists():
                    return path
        except OSError:
            continue

    found = shutil.which("chrome")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def open_in_browser(url: str, reuse_title: str | None = None) -> tuple[bool, str]:
    """Open a URL, reusing an already-open Chrome tab when there is one.

    reuse_title is matched against tab titles; when a tab for the same map is
    already open it is focused and navigated in place, rather than adding yet
    another duplicate tab.
    """
    if reuse_title:
        try:
            from .browser_tabs import focus_existing_tab

            if focus_existing_tab(reuse_title, url):
                return True, f"reused existing tab ({reuse_title})"
        except Exception:
            pass  # fall through to opening a new tab

    chrome = find_chrome()
    if chrome:
        try:
            subprocess.Popen([chrome, url], close_fds=True)
            return True, f"Chrome ({chrome})"
        except Exception:
            pass
    import webbrowser

    if webbrowser.open(url):
        return True, "default browser (highlighting needs Chrome or Edge)"
    return False, "could not open a browser"
