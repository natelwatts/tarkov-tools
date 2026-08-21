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


def search(conn: sqlite3.Connection, term: str, limit: int = 40) -> list[dict[str, Any]]:
    """Find items by name. Returns [{id, name, short_name, kind}]."""
    term = (term or "").strip()
    if not term:
        return []

    rows: list[sqlite3.Row] = []
    fts = _fts_query(term)
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
                (fts, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []

    if not rows:
        like = f"%{term}%"
        rows = conn.execute(
            """
            SELECT id, name, short_name, avg_24h_price
            FROM items
            WHERE name LIKE ? OR short_name LIKE ? OR normalized_name LIKE ?
            ORDER BY LENGTH(name)
            LIMIT ?
            """,
            (like, like, like, limit),
        ).fetchall()

    results = []
    for row in rows:
        results.append(
            {
                "id": row["id"],
                "name": row["name"],
                "short_name": row["short_name"],
                "avg_24h_price": row["avg_24h_price"],
                "kind": item_kind(conn, row["id"]),
            }
        )
    # Surface guns/ammo/mags above generic items.
    # Extraction points share the search bar with items. They live in their
    # own table rather than the item FTS index, so they are queried separately
    # and merged in.
    from . import extracts as extracts_mod

    try:
        for row in extracts_mod.search(conn, term, limit):
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
        if name == lowered:
            quality = 0
        elif name.startswith(lowered):
            quality = 1
        elif lowered in name:
            quality = 2
        else:
            quality = 3
        kind_order = {"weapon": 0, "ammo": 1, "magazine": 2, "extract": 3, "item": 4}
        return (quality, kind_order.get(entry["kind"], 9), len(entry.get("name") or ""))

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
