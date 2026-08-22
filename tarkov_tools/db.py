"""Local SQLite store for Tarkov items, ammo, weapons and magazines.

The point of this database is the compatibility graph:

    weapon --allowedAmmo-->  ammo
    weapon --mod_magazine--> magazine     (from slot filters)
    magazine --allowedAmmo-> ammo

All three edges come straight from the game's own item templates by way of
the tarkov.dev API, so "what fits what" is authoritative rather than guessed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS items (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    short_name      TEXT,
    normalized_name TEXT,
    types           TEXT,          -- JSON array
    base_price      INTEGER,
    avg_24h_price   INTEGER,
    low_24h_price   INTEGER,
    last_low_price  INTEGER,
    wiki_link       TEXT,
    icon_link       TEXT,
    width           INTEGER,
    height          INTEGER,
    weight          REAL,
    kind            TEXT           -- weapon/ammo/part/med/key/... never null after import
);
CREATE INDEX IF NOT EXISTS idx_items_norm ON items(normalized_name);

CREATE TABLE IF NOT EXISTS ammo (
    item_id                    TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    caliber                    TEXT,
    ammo_type                  TEXT,
    damage                     INTEGER,
    projectile_count           INTEGER,
    penetration_power          INTEGER,
    penetration_power_deviation REAL,
    armor_damage               INTEGER,
    fragmentation_chance       REAL,
    ricochet_chance            REAL,
    penetration_chance         REAL,
    accuracy_modifier          REAL,
    recoil_modifier            REAL,
    initial_speed              REAL,
    light_bleed_modifier       REAL,
    heavy_bleed_modifier       REAL,
    stack_max_size             INTEGER,
    tracer                     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ammo_caliber ON ammo(caliber);
CREATE INDEX IF NOT EXISTS idx_ammo_pen ON ammo(penetration_power DESC);

CREATE TABLE IF NOT EXISTS weapons (
    item_id            TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    caliber            TEXT,
    default_ammo_id    TEXT,
    default_preset_id  TEXT,
    ergonomics         REAL,
    recoil_vertical    INTEGER,
    recoil_horizontal  INTEGER,
    fire_rate          INTEGER,
    fire_modes         TEXT,       -- JSON array
    effective_distance INTEGER,
    sighting_range     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_weapons_caliber ON weapons(caliber);

CREATE TABLE IF NOT EXISTS magazines (
    item_id             TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    capacity            INTEGER,
    ergonomics          REAL,
    recoil_modifier     REAL,
    load_modifier       REAL,
    ammo_check_modifier REAL,
    malfunction_chance  REAL
);
CREATE INDEX IF NOT EXISTS idx_mag_capacity ON magazines(capacity DESC);

-- compatibility edges
CREATE TABLE IF NOT EXISTS weapon_ammo (
    weapon_id TEXT NOT NULL,
    ammo_id   TEXT NOT NULL,
    PRIMARY KEY (weapon_id, ammo_id)
);
CREATE INDEX IF NOT EXISTS idx_wa_ammo ON weapon_ammo(ammo_id);

CREATE TABLE IF NOT EXISTS magazine_ammo (
    magazine_id TEXT NOT NULL,
    ammo_id     TEXT NOT NULL,
    PRIMARY KEY (magazine_id, ammo_id)
);
CREATE INDEX IF NOT EXISTS idx_ma_ammo ON magazine_ammo(ammo_id);

CREATE TABLE IF NOT EXISTS weapon_magazine (
    weapon_id   TEXT NOT NULL,
    magazine_id TEXT NOT NULL,
    slot_name   TEXT,
    PRIMARY KEY (weapon_id, magazine_id)
);
CREATE INDEX IF NOT EXISTS idx_wm_mag ON weapon_magazine(magazine_id);

-- Attachment stats. Any item that modifies a weapon carries these.
CREATE TABLE IF NOT EXISTS mods (
    item_id           TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    ergonomics        REAL,
    recoil_modifier   REAL,
    accuracy_modifier REAL,
    loudness          REAL,
    velocity          REAL,
    weight            REAL
);

-- Every weapon/slot/part edge, not just magazines. slot_name is the game's
-- internal name (mod_muzzle, mod_scope, ...); parent_id is the item the slot
-- belongs to, which is the weapon for top-level slots and a part for nested
-- ones such as a receiver's scope rail.
CREATE TABLE IF NOT EXISTS item_slots (
    parent_id  TEXT NOT NULL,
    slot_name  TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    required   INTEGER,
    PRIMARY KEY (parent_id, slot_name, item_id)
);
CREATE INDEX IF NOT EXISTS idx_slots_parent ON item_slots(parent_id, slot_name);
CREATE INDEX IF NOT EXISTS idx_slots_item ON item_slots(item_id);

CREATE TABLE IF NOT EXISTS trader_offers (
    item_id    TEXT NOT NULL,
    vendor     TEXT NOT NULL,
    min_level  INTEGER,
    price      INTEGER,
    currency   TEXT,
    price_rub  INTEGER,
    PRIMARY KEY (item_id, vendor, currency)
);
CREATE INDEX IF NOT EXISTS idx_offers_item ON trader_offers(item_id);

-- Your own lists. Deliberately a separate table so that re-importing
-- templates or re-syncing an account never disturbs them.
--   have  - parts and items sitting in your stash
--   watch - things to look out for in raid
CREATE TABLE IF NOT EXISTS stash (
    item_id   TEXT NOT NULL,
    list_name TEXT NOT NULL,
    quantity  INTEGER NOT NULL DEFAULT 1,
    note      TEXT,
    added_at  TEXT,
    PRIMARY KEY (item_id, list_name)
);
CREATE INDEX IF NOT EXISTS idx_stash_list ON stash(list_name);

CREATE TABLE IF NOT EXISTS flea_prices (
    item_id TEXT PRIMARY KEY,
    price   INTEGER,
    low     INTEGER,
    high    INTEGER,
    oldest  INTEGER,
    points  INTEGER,
    updated INTEGER
);
CREATE INDEX IF NOT EXISTS idx_flea_price ON flea_prices(price);

CREATE TABLE IF NOT EXISTS market_pages (
    slug    TEXT PRIMARY KEY,
    url     TEXT NOT NULL,
    section TEXT
);

CREATE TABLE IF NOT EXISTS recent_searches (
    term      TEXT PRIMARY KEY,
    uses      INTEGER NOT NULL DEFAULT 1,
    last_used TEXT
);

CREATE TABLE IF NOT EXISTS ammo_boxes (
    box_id  TEXT PRIMARY KEY,
    ammo_id TEXT NOT NULL,
    rounds  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ammo_boxes_ammo ON ammo_boxes(ammo_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Full text search over item names for the popover.
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    id UNINDEXED,
    name,
    short_name,
    normalized_name,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that arrived after a database was first created.

    CREATE TABLE IF NOT EXISTS silently does nothing for an existing table, so
    new columns need adding explicitly or older databases break.
    """
    stash_columns = {row[1] for row in conn.execute("PRAGMA table_info(stash)")}
    if stash_columns and "quantity" not in stash_columns:
        # Existing marks predate counting and mean "I have one".
        conn.execute("ALTER TABLE stash ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
        conn.commit()

    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if "kind" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN kind TEXT")
        conn.commit()

    # Whole tables added later need the same treatment as columns: the schema
    # above only runs against a database being created for the first time.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS flea_prices (
            item_id TEXT PRIMARY KEY,
            price   INTEGER,
            low     INTEGER,
            high    INTEGER,
            oldest  INTEGER,
            points  INTEGER,
            updated INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_flea_price ON flea_prices(price);
        CREATE TABLE IF NOT EXISTS market_pages (
            slug    TEXT PRIMARY KEY,
            url     TEXT NOT NULL,
            section TEXT
        );
        CREATE TABLE IF NOT EXISTS recent_searches (
            term      TEXT PRIMARY KEY,
            uses      INTEGER NOT NULL DEFAULT 1,
            last_used TEXT
        );
        CREATE TABLE IF NOT EXISTS ammo_boxes (
            box_id  TEXT PRIMARY KEY,
            ammo_id TEXT NOT NULL,
            rounds  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ammo_boxes_ammo ON ammo_boxes(ammo_id);
    """)
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


# --- writes ------------------------------------------------------------

def upsert_item(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO items (id, name, short_name, normalized_name, types, base_price,
                           avg_24h_price, low_24h_price, last_low_price, wiki_link,
                           icon_link, width, height, weight)
        VALUES (:id, :name, :short_name, :normalized_name, :types, :base_price,
                :avg_24h_price, :low_24h_price, :last_low_price, :wiki_link,
                :icon_link, :width, :height, :weight)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            short_name = excluded.short_name,
            normalized_name = excluded.normalized_name,
            types = excluded.types,
            base_price = excluded.base_price,
            avg_24h_price = excluded.avg_24h_price,
            low_24h_price = excluded.low_24h_price,
            last_low_price = excluded.last_low_price,
            wiki_link = excluded.wiki_link,
            icon_link = excluded.icon_link,
            width = excluded.width,
            height = excluded.height,
            weight = excluded.weight
        """,
        {
            "id": item["id"],
            "name": item.get("name"),
            "short_name": item.get("shortName"),
            "normalized_name": item.get("normalizedName"),
            "types": json.dumps(item.get("types") or []),
            "base_price": item.get("basePrice"),
            "avg_24h_price": item.get("avg24hPrice"),
            "low_24h_price": item.get("low24hPrice"),
            "last_low_price": item.get("lastLowPrice"),
            "wiki_link": item.get("wikiLink"),
            "icon_link": item.get("iconLink"),
            "width": item.get("width"),
            "height": item.get("height"),
            "weight": item.get("weight"),
        },
    )


def upsert_offers(conn: sqlite3.Connection, item_id: str, buy_for: Iterable[dict]) -> None:
    for offer in buy_for or []:
        vendor = offer.get("vendor") or {}
        name = vendor.get("name")
        if not name:
            continue
        conn.execute(
            """
            INSERT INTO trader_offers (item_id, vendor, min_level, price, currency, price_rub)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id, vendor, currency) DO UPDATE SET
                min_level = excluded.min_level,
                price = excluded.price,
                price_rub = excluded.price_rub
            """,
            (
                item_id,
                name,
                vendor.get("minTraderLevel"),
                offer.get("price"),
                offer.get("currency") or "",
                offer.get("priceRUB"),
            ),
        )


def rebuild_fts(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM items_fts")
    conn.execute(
        """
        INSERT INTO items_fts (id, name, short_name, normalized_name)
        SELECT id, COALESCE(name, ''), COALESCE(short_name, ''),
               COALESCE(REPLACE(normalized_name, '-', ' '), '')
        FROM items
        """
    )
    return conn.execute("SELECT COUNT(*) AS c FROM items_fts").fetchone()["c"]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "items", "ammo", "weapons", "magazines", "mods",
        "weapon_ammo", "magazine_ammo", "weapon_magazine", "item_slots",
        "trader_offers", "stash",
    ]
    return {
        t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
        for t in tables
    }
