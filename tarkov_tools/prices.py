"""Flea market prices, for deciding what is worth the backpack space.

tarkov.dev's GraphQL API is the community's canonical price source, but it has
been returning "GraphQL server unavailable" for a while, which is what left
every price column in this database empty.

tarkovforge.com publishes a rolling flea snapshot as a plain static JSON file
on its CDN. It is derived from tarkov.dev, so it is the same data one step
removed, and it keeps working while the API does not. Two things make it a
good fit:

  * it is keyed by BSG template id - the same ids the template importer already
    uses - so it joins straight onto `items` with no name matching, which is
    where price scrapers usually go wrong
  * it is one static file, refreshed hourly, so reading it costs the publisher
    a CDN hit rather than a database query

The file holds a short series per item rather than a single number. The last
point is the current price; the spread across the window is what says whether
something is drifting, which matters more than the absolute number when you
are deciding to carry it out.

Items missing from the feed are not gaps - they are the items banned from the
flea market, which is itself worth reporting.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from typing import Any

FEED_URL = (
    "https://storage.googleapis.com/tarkovforge.firebasestorage.app"
    "/price-history/flea-48h.json"
)

USER_AGENT = "tarkov-tools (personal use; https://github.com/natelwatts/tarkov-tools)"

# Refreshed hourly at source, so anything fresher than this is not worth refetching.
STALE_AFTER_SECONDS = 45 * 60


def fetch(url: str = FEED_URL, timeout: float = 30.0) -> dict[str, Any]:
    """Download the snapshot."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _series_stats(series: list) -> tuple[int, int, int, int, int] | None:
    """(latest, low, high, oldest, points) for one item's price series."""
    values = [int(v) for v in series if isinstance(v, (int, float)) and v > 0]
    if not values:
        return None
    return values[-1], min(values), max(values), values[0], len(values)


def store(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, int]:
    """Write a snapshot into the database.

    The latest price is mirrored into `items.avg_24h_price` as well as kept in
    `flea_prices`, so everything that already renders a price picks these up
    without knowing where they came from.
    """
    updated = int(data.get("updated") or time.time() * 1000)
    rows = []
    for item_id, series in (data.get("items") or {}).items():
        stats = _series_stats(series if isinstance(series, list) else [])
        if stats:
            rows.append((item_id, *stats, updated))

    conn.executemany(
        """
        INSERT INTO flea_prices (item_id, price, low, high, oldest, points, updated)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            price = excluded.price, low = excluded.low, high = excluded.high,
            oldest = excluded.oldest, points = excluded.points,
            updated = excluded.updated
        """,
        rows,
    )
    conn.execute(
        """
        UPDATE items SET avg_24h_price = (
            SELECT price FROM flea_prices WHERE flea_prices.item_id = items.id
        )
        WHERE id IN (SELECT item_id FROM flea_prices)
        """
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('flea_updated', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(updated),),
    )
    conn.commit()

    matched = conn.execute(
        "SELECT COUNT(*) FROM flea_prices p JOIN items i ON i.id = p.item_id"
    ).fetchone()[0]
    return {"in_feed": len(rows), "matched_our_items": matched}


def update(conn: sqlite3.Connection, url: str = FEED_URL) -> dict[str, int]:
    return store(conn, fetch(url))


def snapshot_age(conn: sqlite3.Connection) -> float | None:
    """Seconds since the stored snapshot was published, or None if there is none."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'flea_updated'").fetchone()
    if not row or not row[0]:
        return None
    try:
        return max(0.0, time.time() - int(row[0]) / 1000)
    except (TypeError, ValueError):
        return None


def refresh_if_stale(conn: sqlite3.Connection) -> bool:
    """Update only when the stored snapshot has aged out. True if it refetched."""
    age = snapshot_age(conn)
    if age is not None and age < STALE_AFTER_SECONDS:
        return False
    try:
        update(conn)
        return True
    except Exception:
        return False  # stale prices beat no prices


def price_for(conn: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    """Current price and window for one item, with per-slot value worked out.

    Value per slot is the number that actually decides whether something comes
    home: a 200k item filling six slots loses to a 90k item filling one.
    """
    row = conn.execute(
        """
        SELECT p.price, p.low, p.high, p.oldest, p.points, p.updated,
               i.width, i.height, i.kind
        FROM flea_prices p JOIN items i ON i.id = p.item_id
        WHERE p.item_id = ?
        """,
        (item_id,),
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    slots = (out.get("width") or 0) * (out.get("height") or 0)
    out["slots"] = slots or None
    # A built weapon's footprint comes from its attachments - a barrel and
    # stock each widen it - so the bare template says 1x1 for every gun. Per
    # slot would be a confident wrong number, so it is left off for weapons.
    out["per_slot"] = (
        round(out["price"] / slots)
        if slots and out.get("kind") != "weapon"
        else None
    )
    oldest = out.get("oldest") or 0
    out["change"] = out["price"] - oldest if oldest else 0
    out["change_pct"] = (out["change"] / oldest * 100) if oldest else 0.0
    return out


def top_by_slot(conn: sqlite3.Connection, limit: int = 40,
                min_price: int = 0) -> list[dict[str, Any]]:
    """The best value-per-slot items - what to fill a backpack with."""
    rows = conn.execute(
        """
        SELECT i.name, i.short_name, p.price, i.width, i.height,
               p.price / (i.width * i.height) AS per_slot
        FROM flea_prices p JOIN items i ON i.id = p.item_id
        WHERE i.width > 0 AND i.height > 0 AND p.price >= ?
          AND COALESCE(i.kind, '') <> 'weapon' 
        ORDER BY per_slot DESC
        LIMIT ?
        """,
        (min_price, limit),
    ).fetchall()
    return [dict(r) for r in rows]
