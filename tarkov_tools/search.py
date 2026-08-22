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
    """The item's kind, as classified at import time.

    Every item carries one, so nothing shows an empty type. The per-table
    lookups are only a fallback for databases imported before the column
    existed.
    """
    row = conn.execute("SELECT kind FROM items WHERE id = ?", (item_id,)).fetchone()
    if row and row["kind"]:
        return row["kind"]
    for table, kind in (("ammo", "ammo"), ("weapons", "weapon"),
                        ("magazines", "magazine"), ("mods", "part")):
        try:
            hit = conn.execute(
                f"SELECT 1 FROM {table} WHERE item_id = ?", (item_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            continue
        if hit:
            return kind
    return "item"


# Every kind an item can be classified as. Anything not in the special
# tables (weapons/ammo/magazines/extracts) is filtered on items.kind.
ITEM_KINDS = ("weapon", "ammo", "magazine", "part", "gear", "med", "key",
              "barter", "food", "grenade", "ammobox", "container", "knife",
              "map", "money", "special", "info", "item")


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

    if kind in LIST_NAMES:
        # Ordered by what the pile is worth rather than alphabetically - the
        # question being asked of a stash list is usually "what do I sell".
        rows = conn.execute(
            """
            SELECT i.id, i.name, i.short_name, i.avg_24h_price, s.quantity,
                   COALESCE(p.price, 0) * s.quantity AS line_value
            FROM stash s
            JOIN items i ON i.id = s.item_id
            LEFT JOIN flea_prices p ON p.item_id = s.item_id
            WHERE s.list_name = ?
            ORDER BY line_value DESC, i.name LIMIT ?
            """,
            (kind, limit),
        ).fetchall()
        return [dict(r) | {"kind": kind} for r in rows]

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
    if table:
        rows = conn.execute(
            f"SELECT i.id, i.name, i.short_name, i.avg_24h_price "
            f"FROM {table} a JOIN items i ON i.id = a.item_id ORDER BY {order} LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) | {"kind": kind} for r in rows]

    if kind in ITEM_KINDS:
        rows = conn.execute(
            "SELECT id, name, short_name, avg_24h_price FROM items "
            "WHERE kind = ? ORDER BY name LIMIT ?",
            (kind, limit),
        ).fetchall()
        return [dict(r) | {"kind": kind} for r in rows]
    return []


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

    if kind in LIST_NAMES:
        lowered_term = term.lower()
        return [r for r in browse(conn, kind, None, 500)
                if lowered_term in (r["name"] or "").lower()][:limit]

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
                      "needed": 4, "part": 5, "item": 6}
        order = kind_order.get(entry["kind"], 9)
        # Guns, ammo, magazines and extracts outrank attachments and plain
        # items even on a weaker text match: searching "m4a1" wants the rifle,
        # not the "M4A1 front sight" that merely happens to start with it. An
        # exact name match still wins outright, whatever kind it is.
        bucket = 1 if entry["kind"] in ("part", "item") else 0
        return (exact, bucket, quality, order, len(entry.get("name") or ""))

    results.sort(key=rank)
    return results[:limit]


# --- personal lists -----------------------------------------------------

LIST_NAMES = ("have", "watch")


def toggle_list(conn: sqlite3.Connection, item_id: str, list_name: str) -> bool:
    """Add or remove an item from a list. Returns True if it is now listed."""
    existing = conn.execute(
        "SELECT 1 FROM stash WHERE item_id = ? AND list_name = ?", (item_id, list_name)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM stash WHERE item_id = ? AND list_name = ?",
                     (item_id, list_name))
        conn.commit()
        return False
    conn.execute(
        "INSERT INTO stash (item_id, list_name, quantity, added_at) "
        "VALUES (?,?,1,datetime('now'))",
        (item_id, list_name),
    )
    conn.commit()
    return True


def set_quantity(conn: sqlite3.Connection, item_id: str, list_name: str,
                 quantity: int) -> int:
    """Set how many of something is on a list. Zero or less takes it off.

    Returns the quantity now held, so callers can report it without a second
    read.
    """
    quantity = int(quantity)
    if quantity <= 0:
        conn.execute("DELETE FROM stash WHERE item_id = ? AND list_name = ?",
                     (item_id, list_name))
        conn.commit()
        return 0
    conn.execute(
        """
        INSERT INTO stash (item_id, list_name, quantity, added_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(item_id, list_name) DO UPDATE SET quantity = excluded.quantity
        """,
        (item_id, list_name, quantity),
    )
    conn.commit()
    return quantity


def adjust_quantity(conn: sqlite3.Connection, item_id: str, list_name: str,
                    delta: int) -> int:
    """Nudge a count up or down, adding the item if it was not listed."""
    row = conn.execute(
        "SELECT quantity FROM stash WHERE item_id = ? AND list_name = ?",
        (item_id, list_name),
    ).fetchone()
    current = (row["quantity"] if row else 0) or 0
    return set_quantity(conn, item_id, list_name, current + delta)


def quantities(conn: sqlite3.Connection, list_name: str) -> dict[str, int]:
    """item id -> how many, for drawing counts alongside the marks."""
    try:
        return {r["item_id"]: (r["quantity"] or 1) for r in conn.execute(
            "SELECT item_id, quantity FROM stash WHERE list_name = ?", (list_name,))}
    except sqlite3.Error:
        return {}


# --- notes --------------------------------------------------------------

def notes(conn: sqlite3.Connection, kind: str) -> list[str]:
    """Everything saved under one kind, oldest first."""
    try:
        # rowid breaks the tie rather than the text: two notes saved in the
        # same instant would otherwise sort by case, putting "Penicillin"
        # above "peanuts" for no reason a reader could see.
        return [r["text"] for r in conn.execute(
            "SELECT text FROM notes WHERE kind = ? ORDER BY added_at, rowid",
            (kind,))]
    except sqlite3.Error:
        return []


def add_note(conn: sqlite3.Connection, kind: str, text: str) -> bool:
    """Save one note. False if it was already there, so callers can say so."""
    text = (text or "").strip()
    if not text:
        return False
    existing = conn.execute(
        "SELECT 1 FROM notes WHERE kind = ? AND text = ? COLLATE NOCASE",
        (kind, text),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        "INSERT INTO notes (kind, text, added_at) "
        "VALUES (?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))",
        (kind, text),
    )
    conn.commit()
    return True


def remove_note(conn: sqlite3.Connection, kind: str, text: str) -> str | None:
    """Delete one note, matched without case. Returns what was removed."""
    row = conn.execute(
        "SELECT text FROM notes WHERE kind = ? AND text = ? COLLATE NOCASE",
        (kind, (text or "").strip()),
    ).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM notes WHERE kind = ? AND text = ?",
                 (kind, row["text"]))
    conn.commit()
    return row["text"]


def stash_contents(conn: sqlite3.Connection, list_name: str = "have",
                   limit: int = 500) -> list[dict[str, Any]]:
    """Everything on a list with what it is worth, most valuable line first."""
    try:
        rows = conn.execute(
            """
            SELECT i.id, i.name, i.short_name, s.quantity,
                   p.price AS unit_price,
                   COALESCE(p.price, 0) * s.quantity AS line_value,
                   i.width, i.height
            FROM stash s
            JOIN items i ON i.id = s.item_id
            LEFT JOIN flea_prices p ON p.item_id = s.item_id
            WHERE s.list_name = ?
            ORDER BY line_value DESC, i.name
            LIMIT ?
            """,
            (list_name, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def record_search(conn: sqlite3.Connection, term: str) -> None:
    """Remember a term that was actually used.

    Recorded when a result is opened rather than on every keystroke -
    otherwise the list fills with "l", "le", "led" on the way to "ledx".
    """
    term = (term or "").strip()
    if len(term) < 2:
        return
    try:
        conn.execute(
            """
            INSERT INTO recent_searches (term, uses, last_used)
            VALUES (?, 1, datetime('now'))
            ON CONFLICT(term) DO UPDATE SET
                uses = uses + 1, last_used = datetime('now')
            """,
            (term,),
        )
        conn.commit()
    except sqlite3.Error:
        pass


def recent_searches(conn: sqlite3.Connection, limit: int = 12) -> list[dict[str, Any]]:
    """Terms used lately, most recent first."""
    try:
        rows = conn.execute(
            "SELECT term, uses FROM recent_searches ORDER BY last_used DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {"id": f"recent:{r['term']}", "name": r["term"], "short_name": None,
         "avg_24h_price": None, "kind": "recent", "uses": r["uses"]}
        for r in rows
    ]


def forget_search(conn: sqlite3.Connection, term: str) -> None:
    try:
        conn.execute("DELETE FROM recent_searches WHERE term = ?", (term,))
        conn.commit()
    except sqlite3.Error:
        pass


def listed(conn: sqlite3.Connection, list_name: str) -> set[str]:
    """Item ids on a list, as a set for cheap membership tests while drawing."""
    try:
        return {r["item_id"] for r in conn.execute(
            "SELECT item_id FROM stash WHERE list_name = ?", (list_name,))}
    except sqlite3.Error:
        return set()


def lists_for(conn: sqlite3.Connection, item_id: str) -> set[str]:
    try:
        return {r["list_name"] for r in conn.execute(
            "SELECT list_name FROM stash WHERE item_id = ?", (item_id,))}
    except sqlite3.Error:
        return set()


# --- attachment slots ---------------------------------------------------

# The game numbers repeated slots - mod_tactical, mod_tactical_001,
# mod_tactical002 - but they are the same kind of attachment point, so they
# are collapsed for display.
_SLOT_SUFFIX = re.compile(r"[_]?\d+$")

SLOT_LABELS = {
    "mod_magazine": "Magazine", "mod_muzzle": "Muzzle", "mod_scope": "Scope",
    "mod_stock": "Stock", "mod_handguard": "Handguard", "mod_barrel": "Barrel",
    "mod_foregrip": "Foregrip", "mod_pistol_grip": "Pistol grip",
    "mod_reciever": "Receiver", "mod_tactical": "Tactical", "mod_mount": "Mount",
    "mod_sight_front": "Front sight", "mod_sight_rear": "Rear sight",
    "mod_charge": "Charging handle", "mod_gas_block": "Gas block",
    "mod_bipod": "Bipod", "mod_launcher": "Launcher", "mod_equipment": "Equipment",
    "mod_flashlight": "Flashlight", "mod_trigger": "Trigger",
    "mod_hammer": "Hammer", "mod_catch": "Magazine catch",
    "patron_in_weapon": "Chambered round", "mod_stock_akms": "Stock (AKMS)",
    "mod_stock_axis": "Stock (axis)", "mod_muzzle_000": "Muzzle",
    "mod_muzzle_001": "Muzzle (secondary)",
}


def slot_group(slot_name: str) -> str:
    base = _SLOT_SUFFIX.sub("", slot_name or "")
    return base or slot_name


def slot_label(slot_name: str) -> str:
    group = slot_group(slot_name)
    if group in SLOT_LABELS:
        return SLOT_LABELS[group]
    pretty = group.removeprefix("mod_").replace("_", " ").strip()
    return pretty[:1].upper() + pretty[1:] if pretty else group


def reachable_parts(conn: sqlite3.Connection, root_id: str,
                    max_depth: int = 6) -> dict[str, set[str]]:
    """{slot group: {part id}} for everything that can end up on a weapon.

    Slots nest: an M4A1 has no muzzle slot of its own - the muzzle hangs off
    the receiver's barrel - so only walking direct slots would wrongly report
    that the gun takes no suppressor. The walk is depth-limited and tracks
    visited parts, since rails and mounts can otherwise cycle.
    """
    groups: dict[str, set[str]] = {}
    seen = {root_id}
    frontier = [(root_id, 0)]
    while frontier:
        node, depth = frontier.pop()
        if depth >= max_depth:
            continue
        for row in conn.execute(
            "SELECT slot_name, item_id FROM item_slots WHERE parent_id = ?", (node,)
        ):
            groups.setdefault(slot_group(row["slot_name"]), set()).add(row["item_id"])
            if row["item_id"] not in seen:
                seen.add(row["item_id"])
                frontier.append((row["item_id"], depth + 1))
    return groups


# Not a real attachment slot - a way into the rounds a gun fires, which is
# the first thing worth knowing about a weapon you are holding.
AMMO_SLOT = "__ammo__"


def weapon_slots(conn: sqlite3.Connection, weapon_id: str) -> list[dict]:
    """What goes with a weapon: its ammo first, then attachment categories.

    Ammo leads because "what do I feed it, and what is that worth" is the
    question a gun raises in a raid, where which foregrip fits is not.
    """
    groups = reachable_parts(conn, weapon_id)
    out = [
        {"slot": group, "label": slot_label(group), "count": len(ids)}
        for group, ids in groups.items()
        # The chamber is ammo, offered as its own section below.
        if group != "patron_in_weapon"
    ]
    out.sort(key=lambda entry: (-entry["count"], entry["label"]))

    rounds = len(ammo_for_weapon(conn, weapon_id))
    if rounds:
        out.insert(0, {"slot": AMMO_SLOT, "label": "Ammo", "count": rounds})
    return out


def slot_entries(conn: sqlite3.Connection, weapon_id: str, slot: str) -> list[dict]:
    """The rows behind one section - rounds for ammo, parts for anything else."""
    if slot == AMMO_SLOT:
        return ammo_for_weapon(conn, weapon_id)
    return parts_for_slot(conn, weapon_id, slot)


def guns_for_part(conn: sqlite3.Connection, part_id: str,
                  max_depth: int = 8) -> list[dict]:
    """Every weapon this part can end up on.

    The inverse of reachable_parts: walk parent edges upward until weapons are
    reached. A muzzle device attaches to a barrel, which attaches to a
    receiver, which attaches to the gun - so the answer is several levels up,
    not one.
    """
    weapons = {r["item_id"] for r in conn.execute("SELECT item_id FROM weapons")}
    seen = {part_id}
    frontier = [part_id]
    hits: set[str] = set()
    slots_used: set[str] = set()
    for _ in range(max_depth):
        if not frontier:
            break
        nxt: list[str] = []
        for node in frontier:
            for row in conn.execute(
                "SELECT DISTINCT parent_id, slot_name FROM item_slots WHERE item_id = ?",
                (node,),
            ):
                if node == part_id:
                    slots_used.add(slot_group(row["slot_name"]))
                parent = row["parent_id"]
                if parent in weapons:
                    hits.add(parent)
                if parent not in seen:
                    seen.add(parent)
                    nxt.append(parent)
        frontier = nxt
    if not hits:
        return []
    placeholders = ",".join("?" * len(hits))
    rows = conn.execute(
        f"""SELECT i.id, i.name, w.caliber, w.ergonomics, w.recoil_vertical
            FROM weapons w JOIN items i ON i.id = w.item_id
            WHERE i.id IN ({placeholders}) ORDER BY i.name""",
        tuple(hits),
    ).fetchall()
    out = [dict(r) for r in rows]
    if out:
        out[0]["_slots"] = sorted(slot_label(s) for s in slots_used)
    return out


def parts_for_slot(conn: sqlite3.Connection, weapon_id: str, slot: str,
                   limit: int = 60) -> list[dict]:
    """Every part that fits one slot category, best ergonomics first."""
    ids = reachable_parts(conn, weapon_id).get(slot) or set()
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT i.id, i.name, i.short_name, i.avg_24h_price,
               m.ergonomics, m.recoil_modifier, m.accuracy_modifier,
               m.loudness, m.weight
        FROM items i LEFT JOIN mods m ON m.item_id = i.id
        WHERE i.id IN ({placeholders})
        ORDER BY COALESCE(m.ergonomics, -999) DESC,
                 COALESCE(m.recoil_modifier, 0) ASC, i.name
        LIMIT ?
        """,
        (*ids, limit),
    ).fetchall()
    return [dict(r) for r in rows]


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


def ammo_price(conn, ammo_id: str) -> dict[str, Any] | None:
    """What one round costs, worked back from the pack it is sold in.

    The flea trades ammo by the box, so a single round has no listing of its
    own. The cheapest box per round wins, which is the number that decides
    what you actually load.
    """
    row = conn.execute(
        """
        SELECT b.rounds, p.price AS box_price, i.name AS box_name,
               p.price * 1.0 / b.rounds AS per_round
        FROM ammo_boxes b
        JOIN flea_prices p ON p.item_id = b.box_id
        JOIN items i ON i.id = b.box_id
        WHERE b.ammo_id = ? AND b.rounds > 0
        ORDER BY per_round ASC
        LIMIT 1
        """,
        (ammo_id,),
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["per_round"] = round(out["per_round"])
    return out


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

    out["lists"] = lists_for(conn, item_id)

    # Flea price, when a snapshot has been pulled. Absent from the feed means
    # the item is banned from the flea rather than that we failed to look.
    try:
        from . import prices as prices_mod

        out["flea"] = prices_mod.price_for(conn, item_id)
    except Exception:
        out["flea"] = None
    # Attachments show their modifiers and what they fit. Items are classified
    # as "part" at import now, but older databases only knew "item", so both
    # are accepted - and an attachment without a mods row still gets its
    # weapon list.
    if kind in ("part", "item"):
        mod = conn.execute(
            "SELECT * FROM mods WHERE item_id = ?", (item_id,)
        ).fetchone()
        fits = guns_for_part(conn, item_id)
        if mod or fits:
            out["kind"] = kind = "part"
            out["mod"] = dict(mod) if mod else {}
            out["fits"] = fits

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
        # Rounds are never listed individually, so the price comes from the
        # pack they are sold in.
        out["ammo_price"] = ammo_price(conn, item_id)
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
