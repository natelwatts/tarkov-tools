"""Pull ammo, weapons and magazines from tarkov.dev into the local database.

Run once to build data/tarkov.sqlite3, then re-run to refresh prices. Static
ballistics never change between patches, so a daily sync is plenty.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from . import db as dbmod
from .api import AMMO_QUERY, GUNS_QUERY, MODS_QUERY, ApiUnavailable, TarkovDevClient

# The slot that holds a magazine is consistently named this in the templates.
MAGAZINE_SLOT_NAMES = {"mod_magazine"}


def _ingest_ammo(conn, rows: list[dict[str, Any]]) -> int:
    for row in rows or []:
        item = row.get("item") or {}
        if not item.get("id"):
            continue
        dbmod.upsert_item(conn, item)
        dbmod.upsert_offers(conn, item["id"], item.get("buyFor"))
        conn.execute(
            """
            INSERT INTO ammo (item_id, caliber, ammo_type, damage, projectile_count,
                penetration_power, penetration_power_deviation, armor_damage,
                fragmentation_chance, ricochet_chance, penetration_chance,
                accuracy_modifier, recoil_modifier, initial_speed,
                light_bleed_modifier, heavy_bleed_modifier, stack_max_size, tracer)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET
                caliber=excluded.caliber, ammo_type=excluded.ammo_type,
                damage=excluded.damage, projectile_count=excluded.projectile_count,
                penetration_power=excluded.penetration_power,
                penetration_power_deviation=excluded.penetration_power_deviation,
                armor_damage=excluded.armor_damage,
                fragmentation_chance=excluded.fragmentation_chance,
                ricochet_chance=excluded.ricochet_chance,
                penetration_chance=excluded.penetration_chance,
                accuracy_modifier=excluded.accuracy_modifier,
                recoil_modifier=excluded.recoil_modifier,
                initial_speed=excluded.initial_speed,
                light_bleed_modifier=excluded.light_bleed_modifier,
                heavy_bleed_modifier=excluded.heavy_bleed_modifier,
                stack_max_size=excluded.stack_max_size, tracer=excluded.tracer
            """,
            (
                item["id"], row.get("caliber"), row.get("ammoType"), row.get("damage"),
                row.get("projectileCount"), row.get("penetrationPower"),
                row.get("penetrationPowerDeviation"), row.get("armorDamage"),
                row.get("fragmentationChance"), row.get("ricochetChance"),
                row.get("penetrationChance"), row.get("accuracyModifier"),
                row.get("recoilModifier"), row.get("initialSpeed"),
                row.get("lightBleedModifier"), row.get("heavyBleedModifier"),
                row.get("stackMaxSize"), int(bool(row.get("tracer"))),
            ),
        )
    return len(rows or [])


def _ingest_guns(conn, rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    guns = ammo_edges = mag_edges = 0
    for item in rows or []:
        props = item.get("properties") or {}
        if props.get("__typename") != "ItemPropertiesWeapon":
            continue
        dbmod.upsert_item(conn, item)
        dbmod.upsert_offers(conn, item["id"], item.get("buyFor"))
        conn.execute(
            """
            INSERT INTO weapons (item_id, caliber, default_ammo_id, default_preset_id,
                ergonomics, recoil_vertical, recoil_horizontal, fire_rate, fire_modes,
                effective_distance, sighting_range)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET
                caliber=excluded.caliber, default_ammo_id=excluded.default_ammo_id,
                default_preset_id=excluded.default_preset_id,
                ergonomics=excluded.ergonomics,
                recoil_vertical=excluded.recoil_vertical,
                recoil_horizontal=excluded.recoil_horizontal,
                fire_rate=excluded.fire_rate, fire_modes=excluded.fire_modes,
                effective_distance=excluded.effective_distance,
                sighting_range=excluded.sighting_range
            """,
            (
                item["id"], props.get("caliber"),
                (props.get("defaultAmmo") or {}).get("id"),
                (props.get("defaultPreset") or {}).get("id"),
                props.get("ergonomics"), props.get("recoilVertical"),
                props.get("recoilHorizontal"), props.get("fireRate"),
                json.dumps(props.get("fireModes") or []),
                props.get("effectiveDistance"), props.get("sightingRange"),
            ),
        )
        guns += 1

        for ammo in props.get("allowedAmmo") or []:
            conn.execute(
                "INSERT OR IGNORE INTO weapon_ammo (weapon_id, ammo_id) VALUES (?, ?)",
                (item["id"], ammo["id"]),
            )
            ammo_edges += 1

        for slot in props.get("slots") or []:
            if (slot.get("nameId") or slot.get("name") or "").lower() not in MAGAZINE_SLOT_NAMES:
                continue
            allowed = ((slot.get("filters") or {}).get("allowedItems")) or []
            for mag in allowed:
                conn.execute(
                    "INSERT OR IGNORE INTO weapon_magazine (weapon_id, magazine_id, slot_name) "
                    "VALUES (?, ?, ?)",
                    (item["id"], mag["id"], slot.get("nameId") or slot.get("name")),
                )
                mag_edges += 1
    return guns, ammo_edges, mag_edges


def _ingest_magazines(conn, rows: list[dict[str, Any]]) -> tuple[int, int]:
    mags = edges = 0
    for item in rows or []:
        props = item.get("properties") or {}
        if props.get("__typename") != "ItemPropertiesMagazine":
            continue
        dbmod.upsert_item(conn, item)
        dbmod.upsert_offers(conn, item["id"], item.get("buyFor"))
        conn.execute(
            """
            INSERT INTO magazines (item_id, capacity, ergonomics, recoil_modifier,
                load_modifier, ammo_check_modifier, malfunction_chance)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET
                capacity=excluded.capacity, ergonomics=excluded.ergonomics,
                recoil_modifier=excluded.recoil_modifier,
                load_modifier=excluded.load_modifier,
                ammo_check_modifier=excluded.ammo_check_modifier,
                malfunction_chance=excluded.malfunction_chance
            """,
            (
                item["id"], props.get("capacity"), props.get("ergonomics"),
                props.get("recoilModifier"), props.get("loadModifier"),
                props.get("ammoCheckModifier"), props.get("malfunctionChance"),
            ),
        )
        mags += 1
        for ammo in props.get("allowedAmmo") or []:
            conn.execute(
                "INSERT OR IGNORE INTO magazine_ammo (magazine_id, ammo_id) VALUES (?, ?)",
                (item["id"], ammo["id"]),
            )
            edges += 1
    return mags, edges


def sync(use_cache: bool = False, verbose: bool = True) -> dict[str, int]:
    """Fetch everything and rebuild the local database."""
    client = TarkovDevClient()
    conn = dbmod.connect()

    def log(msg: str) -> None:
        if verbose:
            print(msg)

    try:
        log(f"endpoint: {client.endpoint}  (no API key required)")
        log(f"mode: {client.game_mode}  lang: {client.language}")

        log("\nfetching ammo ...")
        ammo_data = client.fetch("ammo", AMMO_QUERY, use_cache)
        log("fetching guns ...")
        guns_data = client.fetch("guns", GUNS_QUERY, use_cache)
        log("fetching mods (for magazines) ...")
        mods_data = client.fetch("mods", MODS_QUERY, use_cache)
    except ApiUnavailable as exc:
        conn.close()
        raise SystemExit(
            f"\ntarkov.dev is unavailable right now.\n\n{exc}\n\n"
            "Nothing was written. Re-run 'sync' when it recovers, or add\n"
            "--cache to rebuild from a previous successful fetch."
        ) from exc

    with conn:
        log("\ningesting ...")
        n_ammo = _ingest_ammo(conn, ammo_data.get("ammo"))
        log(f"  ammo         {n_ammo}")
        n_guns, wa, wm = _ingest_guns(conn, guns_data.get("items"))
        log(f"  weapons      {n_guns}  (ammo edges {wa}, magazine edges {wm})")
        n_mags, ma = _ingest_magazines(conn, mods_data.get("items"))
        log(f"  magazines    {n_mags}  (ammo edges {ma})")
        indexed = dbmod.rebuild_fts(conn)
        log(f"  search index {indexed}")
        dbmod.set_meta(conn, "last_sync", _dt.datetime.now().isoformat(timespec="seconds"))
        dbmod.set_meta(conn, "game_mode", client.game_mode)

    result = dbmod.counts(conn)
    conn.close()
    return result
