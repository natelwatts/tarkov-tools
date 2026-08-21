"""Search and compatibility lookups over the local database.

This is the part that answers "what ammo, mags and guns actually go together"
without paging through the wiki. Every relationship comes from the game's own
item templates, so the answers are authoritative.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

_TOKEN_RE = re.compile(r"[A-Za-z0-9\.]+")


def _fts_query(term: str) -> str | None:
    """Turn free text into a safe FTS5 prefix query.

    User input can contain characters FTS5 treats as operators, so tokens are
    extracted and quoted rather than passed through.
    """
    tokens = _TOKEN_RE.findall(term or "")
    if not tokens:
        return None
    quoted = [f'"{t}"' for t in tokens[:-1]]
    quoted.append(f'"{tokens[-1]}"*')
    return " ".join(quoted)


def item_kind(conn: sqlite3.Connection, item_id: str) -> str:
    for table, kind in (("ammo", "ammo"), ("weapons", "weapon"), ("magazines", "magazine")):
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE item_id = ?", (item_id,)
        ).fetchone()
        if row:
            return kind
    return "item"


ITEM_KINDS = ("weapon", "ammo", "magazine", "item")


def tracker_configured(conn: sqlite3.Connection) -> bool:
    """True only once a TarkovTracker account has actually been synced.

    The whole account integration is optional - someone who has never
    connected one should not see an empty Needed filter or a table that does
    not exist, so everything quest-related keys off this.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='needed_items'"
        ).fetchone()
        if not row:
            return False
        return bool(conn.execute("SELECT 1 FROM needed_items LIMIT 1").fetchone())
    except sqlite3.Error:
        return False


def browse(conn: sqlite3.Connection, kind: str, side: str | None = None,
           limit: int = 200) -> list[dict[str, Any]]:
    """Everything of one kind, for when the search box is empty.

    Lets a filter double as a browsable list: pick "Ammo" and see the whole
    chart, pick "Scav extracts" and see all of them.
    """
    if kind == "needed":
        # Outstanding requirements, biggest shortfall first. Wide "any of N"
        # objectives are excluded: those are a category, not a shopping item.
        # The table only exists once a TarkovTracker account has been synced;
        # without one this is simply empty rather than an error.
        if not tracker_configured(conn):
            return []
        rows = conn.execute(
            """
            SELECT n.item_id AS id, i.name, i.short_name, i.avg_24h_price,
                   SUM(n.need - n.have) AS outstanding,
                   MAX(n.available) AS available
            FROM needed_items n LEFT JOIN items i ON i.id = n.item_id
            WHERE n.optional = 0 AND n.need > n.have AND n.alternatives <= 3
            GROUP BY n.item_id
            ORDER BY MAX(n.available) DESC, outstanding DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {"id": r["id"], "name": r["name"] or r["id"],
             # Locked means a prerequisite task or level gate is not met yet,
             # so it is needed eventually but not actionable now.
             "short_name": (f"need {r['outstanding']}" if r["available"]
                            else f"need {r['outstanding']} (locked)"),
             "avg_24h_price": r["avg_24h_price"], "kind": "needed"}
            for r in rows
        ]

    if kind == "extract":
        clause = " WHERE side = ?" if side else ""
        params: tuple = (side, limit) if side else (limit,)
        rows = conn.execute(
            f"SELECT rowid, display_name, map_name FROM extracts{clause}"
            " ORDER BY map_name, display_name LIMIT ?",
            params,
        ).fetchall()
        return [
            {"id": f"extract:{r['rowid']}", "name": r["display_name"],
             "short_name": r["map_name"], "avg_24h_price": None, "kind": "extract"}
            for r in rows
        ]

    table, order = {
        "weapon": ("weapons", "i.name"),
        "ammo": ("ammo", "a.caliber, a.penetration_power DESC"),
        "magazine": ("magazines", "a.capacity DESC, i.name"),
    }.get(kind, (None, None))
    if not table:
        return []
    rows = conn.execute(
        f"SELECT i.id, i.name, i.short_name, i.avg_24h_price "
        f"FROM {table} a JOIN items i ON i.id = a.item_id ORDER BY {order} LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) | {"kind": kind} for r in rows]


def search(conn: sqlite3.Connection, term: str, limit: int = 40,
           kind: str | None = None, side: str | None = None) -> list[dict[str, Any]]:
    """Find items and extracts by name.

    kind narrows to one of weapon/ammo/magazine/extract; side further narrows
    extracts to Pmc/Scav/Coop. With no term but a kind, the whole category is
    listed instead of nothing.
    """
    term = (term or "").strip()
    if not term:
        # Browsing a whole category should not be capped at a
        # result-list size meant for typed queries.
        return browse(conn, kind, side, max(limit, 300)) if kind else []

    if kind == "needed":
        if not tracker_configured(conn):
            return []
        # Narrow the shopping list by name rather than running a fresh search.
        lowered_term = term.lower()
        return [r for r in browse(conn, "needed", None, 400)
                if lowered_term in (r["name"] or "").lower()][:limit]

    want_items = kind is None or kind in ITEM_KINDS
    want_extracts = kind is None or kind == "extract"
    # Filtering happens after the query, so pull a wider net when narrowing.
    fetch = limit if kind is None else limit * 6

    rows: list[sqlite3.Row] = []
    fts = _fts_query(term) if want_items else None
    if fts:
        try:
            rows = conn.execute(
                """
                SELECT f.id, i.name, i.short_name, i.avg_24h_price
                FROM items_fts f
                JOIN items i ON i.id = f.id
                WHERE items_fts MATCH ?
                ORDER BY bm25(items_fts), LENGTH(i.name)
                LIMIT ?
                """,
                (fts, fetch),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []

    if want_items and not rows:
        like = f"%{term}%"
        rows = conn.execute(
            """
            SELECT id, name, short_name, avg_24h_price
            FROM items
            WHERE name LIKE ? OR short_name LIKE ? OR normalized_name LIKE ?
            ORDER BY LENGTH(name)
            LIMIT ?
            """,
            (like, like, like, fetch),
        ).fetchall()

    results = []
    for row in rows:
        row_kind = item_kind(conn, row["id"])
        if kind is not None and row_kind != kind:
            continue
        results.append(
            {
                "id": row["id"],
                "name": row["name"],
                "short_name": row["short_name"],
                "avg_24h_price": row["avg_24h_price"],
                "kind": row_kind,
            }
        )
    # Surface guns/ammo/mags above generic items.
    # Extraction points share the search bar with items. They live in their
    # own table rather than the item FTS index, so they are queried separately
    # and merged in.
    from . import extracts as extracts_mod

    try:
        for row in (extracts_mod.search(conn, term, fetch, side=side)
                    if want_extracts else []):
            results.append(
                {
                    "id": f"extract:{row['rowid']}",
                    "name": row.get("display_name") or row.get("name"),
                    "short_name": row.get("map_name"),
                    "avg_24h_price": None,
                    "kind": "extract",
                }
            )
    except sqlite3.OperationalError:
        pass  # no extract data imported yet

    lowered = term.lower()

    def rank(entry: dict) -> tuple:
        name = (entry.get("name") or "").lower()
        exact = name != lowered  # False sorts first
        if name.startswith(lowered):
            quality = 0
        elif lowered in name:
            quality = 1
        else:
            quality = 2
        kind_order = {"weapon": 0, "ammo": 1, "magazine": 2, "extract": 3,
                      "needed": 4, "item": 5}
        order = kind_order.get(entry["kind"], 9)
        # Guns, ammo, magazines and extracts outrank plain items even on a
        # weaker text match: searching "m4a1" wants the rifle, not the
        # "M4A1 upper receiver" that merely happens to start with it. An exact
        # name match still wins outright, whatever kind it is.
        bucket = 1 if entry["kind"] == "item" else 0
        return (exact, bucket, quality, order, len(entry.get("name") or ""))

    results.sort(key=rank)
    return results[:limit]


# --- relationship queries ----------------------------------------------

_AMMO_COLUMNS = """
    i.id, i.name, i.short_name, i.avg_24h_price,
    a.caliber, a.damage, a.penetration_power, a.armor_damage,
    a.fragmentation_chance, a.initial_speed, a.projectile_count
"""

_MAG_COLUMNS = """
    i.id, i.name, i.short_name, i.avg_24h_price,
    m.capacity, m.ergonomics, m.load_modifier, m.malfunction_chance
"""

_GUN_COLUMNS = """
    i.id, i.name, i.short_name, i.avg_24h_price,
    w.caliber, w.ergonomics, w.recoil_vertical, w.recoil_horizontal, w.fire_rate
"""


def ammo_for_weapon(conn, weapon_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"""SELECT {_AMMO_COLUMNS}
            FROM weapon_ammo wa
            JOIN ammo a ON a.item_id = wa.ammo_id
            JOIN items i ON i.id = a.item_id
            WHERE wa.weapon_id = ?
            ORDER BY a.penetration_power DESC, a.damage DESC""",
        (weapon_id,),
    )]


def magazines_for_weapon(conn, weapon_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"""SELECT {_MAG_COLUMNS}
            FROM weapon_magazine wm
            JOIN magazines m ON m.item_id = wm.magazine_id
            JOIN items i ON i.id = m.item_id
            WHERE wm.weapon_id = ?
            ORDER BY m.capacity DESC, i.name""",
        (weapon_id,),
    )]


def weapons_for_ammo(conn, ammo_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"""SELECT {_GUN_COLUMNS}
            FROM weapon_ammo wa
            JOIN weapons w ON w.item_id = wa.weapon_id
            JOIN items i ON i.id = w.item_id
            WHERE wa.ammo_id = ?
            ORDER BY i.name""",
        (ammo_id,),
    )]


def magazines_for_ammo(conn, ammo_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"""SELECT {_MAG_COLUMNS}
            FROM magazine_ammo ma
            JOIN magazines m ON m.item_id = ma.magazine_id
            JOIN items i ON i.id = m.item_id
            WHERE ma.ammo_id = ?
            ORDER BY m.capacity DESC, i.name""",
        (ammo_id,),
    )]


def ammo_for_magazine(conn, magazine_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"""SELECT {_AMMO_COLUMNS}
            FROM magazine_ammo ma
            JOIN ammo a ON a.item_id = ma.ammo_id
            JOIN items i ON i.id = a.item_id
            WHERE ma.magazine_id = ?
            ORDER BY a.penetration_power DESC""",
        (magazine_id,),
    )]


def weapons_for_magazine(conn, magazine_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"""SELECT {_GUN_COLUMNS}
            FROM weapon_magazine wm
            JOIN weapons w ON w.item_id = wm.weapon_id
            JOIN items i ON i.id = w.item_id
            WHERE wm.magazine_id = ?
            ORDER BY i.name""",
        (magazine_id,),
    )]


def offers_for(conn, item_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT vendor, min_level, price, currency, price_rub
           FROM trader_offers WHERE item_id = ?
           ORDER BY COALESCE(price_rub, price)""",
        (item_id,),
    )]


def describe(conn: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    """Full detail for one result, shaped by what kind of thing it is."""
    if isinstance(item_id, str) and item_id.startswith("extract:"):
        rowid = item_id.split(":", 1)[1]
        row = conn.execute(
            "SELECT rowid, * FROM extracts WHERE rowid = ?", (rowid,)
        ).fetchone()
        return {"kind": "extract", "extract": dict(row)} if row else {}

    base = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not base:
        return {}
    kind = item_kind(conn, item_id)
    out: dict[str, Any] = {"kind": kind, "item": dict(base), "offers": offers_for(conn, item_id)}

    # What still wants this item - only if a TarkovTracker account is set up.
    out["needs"] = []
    if tracker_configured(conn):
        try:
            from . import tracker as tracker_mod

            out["needs"] = tracker_mod.needs_for_item(conn, item_id)
        except Exception:
            pass

    if kind == "ammo":
        stats = conn.execute("SELECT * FROM ammo WHERE item_id = ?", (item_id,)).fetchone()
        out["stats"] = dict(stats) if stats else {}
        out["weapons"] = weapons_for_ammo(conn, item_id)
        out["magazines"] = magazines_for_ammo(conn, item_id)
    elif kind == "weapon":
        stats = conn.execute("SELECT * FROM weapons WHERE item_id = ?", (item_id,)).fetchone()
        out["stats"] = dict(stats) if stats else {}
        out["ammo"] = ammo_for_weapon(conn, item_id)
        out["magazines"] = magazines_for_weapon(conn, item_id)
    elif kind == "magazine":
        stats = conn.execute("SELECT * FROM magazines WHERE item_id = ?", (item_id,)).fetchone()
        out["stats"] = dict(stats) if stats else {}
        out["ammo"] = ammo_for_magazine(conn, item_id)
        out["weapons"] = weapons_for_magazine(conn, item_id)
    return out


def ammo_chart(conn: sqlite3.Connection, caliber: str | None = None) -> list[dict]:
    """Every round, best penetration first. Optionally filtered to one caliber."""
    if caliber:
        rows = conn.execute(
            f"""SELECT {_AMMO_COLUMNS} FROM ammo a JOIN items i ON i.id = a.item_id
                WHERE a.caliber = ? ORDER BY a.penetration_power DESC""",
            (caliber,),
        )
    else:
        rows = conn.execute(
            f"""SELECT {_AMMO_COLUMNS} FROM ammo a JOIN items i ON i.id = a.item_id
                ORDER BY a.caliber, a.penetration_power DESC"""
        )
    return [dict(r) for r in rows]


def calibers(conn: sqlite3.Connection) -> list[str]:
    return [
        r["caliber"]
        for r in conn.execute(
            "SELECT DISTINCT caliber FROM ammo WHERE caliber IS NOT NULL ORDER BY caliber"
        )
    ]
