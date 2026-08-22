"""Deep links to an item's flea market page.

tarkovforge's /market page is a single search UI that reads nothing from the
URL, so there is no way to hand it a search term. What it does have is a
prerendered page per item, but under a category route rather than under
/market - an armour goes to /armor/<slug>, a scope to /attachments/<slug> -
and nothing in the page tells you which category an item landed in.

Its sitemap does. It lists every prerendered route, so one fetch gives a
complete slug-to-URL map, cached in the database afterwards. Matching is by
slug rather than by name, which sidesteps the usual near-miss guessing: a
slug either exists or it does not.

Their slug rule drops dots and slashes instead of turning them into
separators - "5.56x45mm M855" becomes "556x45mm-m855" and "12/70" becomes
"1270" - which is worth knowing, because treating them as separators matches
barely half of what treating them as nothing does.

Coverage is good for the things whose price you actually look up mid-raid
(parts, magazines, weapons, ammo, keys, meds) and patchy for gear, where
their site simply has fewer pages. Anything unmatched falls back to the
market search page, which is still the right place to be.
"""

from __future__ import annotations

import re
import sqlite3
import time
import urllib.request

SITEMAP_URL = "https://tarkovforge.com/sitemap.xml"
MARKET_URL = "https://tarkovforge.com/market"

USER_AGENT = "tarkov-tools (personal use; https://github.com/natelwatts/tarkov-tools)"

# Routes that are articles and guides rather than items.
NON_ITEM_SECTIONS = {
    "blog", "bosses", "hideout", "lore", "map-guide", "maps", "skills",
    "story-guide", "task-guide", "traders", "forums",
}

# The sitemap changes when the game patches, not hourly.
STALE_AFTER_SECONDS = 7 * 24 * 3600

_URL_RE = re.compile(r"https://tarkovforge\.com/([a-z-]+)/([a-z0-9-]+)")


def slugify(name: str) -> str:
    """The slug tarkovforge would give this item."""
    text = (name or "").lower().replace("&", " and ")
    text = re.sub(r"[./']", "", text)          # dropped, not separated
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def fetch_sitemap(url: str = SITEMAP_URL, timeout: float = 30.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def store_sitemap(conn: sqlite3.Connection, xml: str) -> int:
    rows = []
    for section, slug in _URL_RE.findall(xml):
        if section in NON_ITEM_SECTIONS:
            continue
        rows.append((slug, f"https://tarkovforge.com/{section}/{slug}", section))
    conn.executemany(
        "INSERT INTO market_pages (slug, url, section) VALUES (?, ?, ?) "
        "ON CONFLICT(slug) DO UPDATE SET url = excluded.url, section = excluded.section",
        rows,
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('market_pages_updated', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(int(time.time())), ),
    )
    conn.commit()
    return len(rows)


def refresh(conn: sqlite3.Connection) -> int:
    return store_sitemap(conn, fetch_sitemap())


def _age(conn: sqlite3.Connection) -> float | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'market_pages_updated'"
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return max(0.0, time.time() - int(row[0]))
    except (TypeError, ValueError):
        return None


def refresh_if_stale(conn: sqlite3.Connection) -> bool:
    """Fetch the sitemap if we have never done so, or it has aged out."""
    age = _age(conn)
    if age is not None and age < STALE_AFTER_SECONDS:
        return False
    try:
        refresh(conn)
        return True
    except Exception:
        return False


def url_for(conn: sqlite3.Connection, name: str) -> tuple[str, bool]:
    """(url, is_exact) for an item's market page.

    Falls back to the market search page, which is a reasonable place to land
    when their site has no page for the item - and is what was asked for
    anyway.
    """
    slug = slugify(name)
    if not slug:
        return MARKET_URL, False
    refresh_if_stale(conn)
    row = conn.execute("SELECT url FROM market_pages WHERE slug = ?", (slug,)).fetchone()
    if row:
        return row["url"] if hasattr(row, "keys") else row[0], True
    return MARKET_URL, False
