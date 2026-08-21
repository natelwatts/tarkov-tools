"""Import the game's raw item templates directly, with no API involved.

Escape from Tarkov ships an item template database that every client
downloads. Community projects publish dumps of it. This module reads that
raw BSG format and builds the same compatibility graph the tarkov.dev
importer does, entirely offline:

    weapon._props.Chambers[]._props.filters[].Filter          -> weapon accepts ammo
    weapon._props.Slots[_name=mod_magazine].filters[].Filter  -> weapon accepts magazine
    magazine._props.Cartridges[]._props.filters[].Filter      -> magazine accepts ammo

Display names live in a separate locale file, keyed "<id> Name", because the
templates only carry internal names like "patron_556x45_M995".

Templates contain no market prices - they are not in the game files. Price
columns are left untouched here, so a template import can safely refresh
static data without clobbering prices from a previous API sync.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from . import db as dbmod
from .config import DATA_DIR

# Root category ids in the BSG template tree. Items are classified by walking
# their _parent chain, so subcategories (Assault rifle, SMG, ...) are covered.
WEAPON_CATEGORY = "5422acb9af1c889c16000029"
AMMO_CATEGORY = "5485a8684bdc2da71d8b4567"
MAGAZINE_CATEGORY = "5448bc234bdc2d3c308b4569"

USER_AGENT = "tarkov-tools/0.1 (personal, non-commercial)"

# A community mirror of the SPT database. Any dump in the same raw format
# works - override these in config.json if you have a different source.
DEFAULT_ITEMS_URL = (
    "https://raw.githubusercontent.com/paulov-t/Paulov.Tarkov.Db/master"
    "/database/templates/items.json"
)
DEFAULT_LOCALE_URL = (
    "https://raw.githubusercontent.com/paulov-t/Paulov.Tarkov.Db/master"
    "/database/locales/global/en.json"
)

TEMPLATE_DIR = DATA_DIR / "templates"


# --- loading -----------------------------------------------------------

def download(items_url: str | None = None,
             locale_url: str | None = None,
             dest: Path | None = None,
             verbose: bool = True) -> tuple[Path, Path]:
    """Fetch a template dump and its locale file to disk."""
    from .config import load_config

    cfg = load_config().get("templates", {})
    items_url = items_url or cfg.get("items_url") or DEFAULT_ITEMS_URL
    locale_url = locale_url or cfg.get("locale_url") or DEFAULT_LOCALE_URL
    dest = dest or TEMPLATE_DIR
    dest.mkdir(parents=True, exist_ok=True)
    items_path = dest / "items.json"
    locale_path = dest / "en.json"

    for url, path in ((items_url, items_path), (locale_url, locale_path)):
        if verbose:
            print(f"  downloading {path.name} ...", end="", flush=True)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=180) as response:
            path.write_bytes(response.read())
        if verbose:
            print(f" {path.stat().st_size:,} bytes")
    return items_path, locale_path


def load(items_path: Path, locale_path: Path | None = None) -> tuple[dict, dict]:
    templates = json.loads(Path(items_path).read_text(encoding="utf-8"))
    locale: dict[str, Any] = {}
    if locale_path and Path(locale_path).exists():
        locale = json.loads(Path(locale_path).read_text(encoding="utf-8"))
    return templates, locale


# --- helpers -----------------------------------------------------------

def _ancestors(templates: dict, item_id: str, cache: dict[str, list[str]]) -> list[str]:
    if item_id in cache:
        return cache[item_id]
    chain: list[str] = []
    seen = {item_id}
    current = (templates.get(item_id) or {}).get("_parent")
    while current and current in templates and current not in seen:
        chain.append(current)
        seen.add(current)
        current = templates[current].get("_parent")
    cache[item_id] = chain
    return chain


def _filters(container: dict) -> list[str]:
    """Union every allowed-item id out of a slot/chamber/cartridge filter block."""
    out: list[str] = []
    for filter_block in (container.get("_props") or {}).get("filters") or []:
        out.extend(filter_block.get("Filter") or [])
    return out


def _normalize(name: str | None) -> str | None:
    if not name:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or None


def _display_name(item_id: str, props: dict, locale: dict) -> str | None:
    return locale.get(f"{item_id} Name") or props.get("Name") or None


def _short_name(item_id: str, props: dict, locale: dict) -> str | None:
    return locale.get(f"{item_id} ShortName") or props.get("ShortName") or None


def _upsert_item_static(conn, item_id: str, props: dict, locale: dict, types: Iterable[str]) -> None:
    """Insert/update an item WITHOUT touching price columns.

    Templates carry no market data, so a template import must not blank out
    prices previously fetched from the API.
    """
    name = _display_name(item_id, props, locale)
    conn.execute(
        """
        INSERT INTO items (id, name, short_name, normalized_name, types, base_price,
                           wiki_link, icon_link, width, height, weight)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name            = excluded.name,
            short_name      = excluded.short_name,
            normalized_name = excluded.normalized_name,
            types           = excluded.types,
            base_price      = COALESCE(excluded.base_price, items.base_price),
            width           = excluded.width,
            height          = excluded.height,
            weight          = excluded.weight
        """,
        (
            item_id,
            name,
            _short_name(item_id, props, locale),
            _normalize(name),
            json.dumps(list(types)),
            props.get("CreditsPrice"),
            None,
            None,
            props.get("Width"),
            props.get("Height"),
            props.get("Weight"),
        ),
    )


# --- import ------------------------------------------------------------

def import_templates(conn, templates: dict, locale: dict, verbose: bool = True) -> dict[str, int]:
    cache: dict[str, list[str]] = {}

    def in_category(item_id: str, category: str) -> bool:
        return category in _ancestors(templates, item_id, cache)

    real_items = {
        k: v for k, v in templates.items()
        if v.get("_type") == "Item" and isinstance(v.get("_props"), dict)
    }

    ammo_ids = {k for k in real_items if in_category(k, AMMO_CATEGORY)}
    mag_ids = {k for k in real_items if in_category(k, MAGAZINE_CATEGORY)}
    weapon_ids = {k for k in real_items if in_category(k, WEAPON_CATEGORY)}

    if verbose:
        print(f"  classified: {len(weapon_ids)} weapons, {len(ammo_ids)} ammo, "
              f"{len(mag_ids)} magazines")

    # --- ammo
    for item_id in ammo_ids:
        props = real_items[item_id]["_props"]
        _upsert_item_static(conn, item_id, props, locale, ["ammo"])
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
                item_id, props.get("Caliber"), props.get("ammoType"),
                props.get("Damage"), props.get("ProjectileCount"),
                props.get("PenetrationPower"),
                # BSG's own spelling of "Deviation".
                props.get("PenetrationPowerDiviation"),
                props.get("ArmorDamage"), props.get("FragmentationChance"),
                props.get("RicochetChance"), props.get("PenetrationChanceObstacle"),
                props.get("ammoAccr"), props.get("ammoRec"),
                props.get("InitialSpeed"), props.get("LightBleedingDelta"),
                props.get("HeavyBleedingDelta"), props.get("StackMaxSize"),
                int(bool(props.get("Tracer"))),
            ),
        )

    # --- magazines (and their ammo edges)
    magazine_ammo_edges = 0
    for item_id in mag_ids:
        props = real_items[item_id]["_props"]
        _upsert_item_static(conn, item_id, props, locale, ["mods", "magazine"])
        cartridges = props.get("Cartridges") or []
        capacity = cartridges[0].get("_max_count") if cartridges else None
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
                item_id, capacity, props.get("Ergonomics"), props.get("Recoil"),
                props.get("LoadUnloadModifier"), props.get("CheckTimeModifier"),
                props.get("MalfunctionChance"),
            ),
        )
        for cartridge in cartridges:
            for ammo_id in _filters(cartridge):
                if ammo_id in ammo_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO magazine_ammo (magazine_id, ammo_id) "
                        "VALUES (?, ?)",
                        (item_id, ammo_id),
                    )
                    magazine_ammo_edges += 1

    # --- weapons (and their ammo/magazine edges)
    weapon_ammo_edges = weapon_mag_edges = 0
    for item_id in weapon_ids:
        props = real_items[item_id]["_props"]
        _upsert_item_static(conn, item_id, props, locale, ["gun"])
        fire_modes = props.get("weapFireType") or []
        conn.execute(
            """
            INSERT INTO weapons (item_id, caliber, default_ammo_id, default_preset_id,
                ergonomics, recoil_vertical, recoil_horizontal, fire_rate, fire_modes,
                effective_distance, sighting_range)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET
                caliber=excluded.caliber, default_ammo_id=excluded.default_ammo_id,
                ergonomics=excluded.ergonomics,
                recoil_vertical=excluded.recoil_vertical,
                recoil_horizontal=excluded.recoil_horizontal,
                fire_rate=excluded.fire_rate, fire_modes=excluded.fire_modes
            """,
            (
                item_id, props.get("ammoCaliber"), props.get("defAmmo"), None,
                props.get("Ergonomics"), props.get("RecoilForceUp"),
                props.get("RecoilForceBack"), props.get("bFirerate"),
                json.dumps(fire_modes if isinstance(fire_modes, list) else []),
                None, None,
            ),
        )

        # chambered rounds
        for chamber in props.get("Chambers") or []:
            for ammo_id in _filters(chamber):
                if ammo_id in ammo_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO weapon_ammo (weapon_id, ammo_id) VALUES (?, ?)",
                        (item_id, ammo_id),
                    )
                    weapon_ammo_edges += 1

        # magazines this weapon accepts
        for slot in props.get("Slots") or []:
            if slot.get("_name") != "mod_magazine":
                continue
            for mag_id in _filters(slot):
                if mag_id in mag_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO weapon_magazine "
                        "(weapon_id, magazine_id, slot_name) VALUES (?, ?, ?)",
                        (item_id, mag_id, "mod_magazine"),
                    )
                    weapon_mag_edges += 1

    # A weapon with no chamber filter (or a magazine-fed design that lists
    # nothing directly) still implies ammo through the magazines it takes.
    inferred = conn.execute(
        """
        INSERT OR IGNORE INTO weapon_ammo (weapon_id, ammo_id)
        SELECT DISTINCT wm.weapon_id, ma.ammo_id
        FROM weapon_magazine wm
        JOIN magazine_ammo ma ON ma.magazine_id = wm.magazine_id
        WHERE NOT EXISTS (
            SELECT 1 FROM weapon_ammo wa WHERE wa.weapon_id = wm.weapon_id
        )
        """
    ).rowcount

    if verbose:
        print(f"  edges: weapon->ammo {weapon_ammo_edges} (+{max(inferred, 0)} inferred "
              f"via magazines), weapon->magazine {weapon_mag_edges}, "
              f"magazine->ammo {magazine_ammo_edges}")

    indexed = dbmod.rebuild_fts(conn)
    if verbose:
        print(f"  search index {indexed}")
    return dbmod.counts(conn)


def run_import(items_path: Path | None = None,
               locale_path: Path | None = None,
               do_download: bool = False,
               verbose: bool = True) -> dict[str, int]:
    if do_download or items_path is None:
        items_path, locale_path = download(verbose=verbose)
    elif locale_path is None:
        candidate = Path(items_path).parent / "en.json"
        locale_path = candidate if candidate.exists() else None

    if verbose:
        print(f"  reading {items_path}")
    templates, locale = load(Path(items_path), Path(locale_path) if locale_path else None)
    if verbose:
        print(f"  {len(templates):,} templates, {len(locale):,} locale strings")
        if not locale:
            print("  warning: no locale file, names will be internal "
                  "(e.g. patron_556x45_M995)")

    conn = dbmod.connect()
    with conn:
        counts = import_templates(conn, templates, locale, verbose)
        dbmod.set_meta(conn, "last_template_import", str(Path(items_path)))

        # Extraction points live in the location files rather than the item
        # templates, so they are a separate fetch sharing the same locale.
        from . import extracts as ex

        if verbose:
            print("\nextraction points ...")
        markers = {}
        if do_download:
            ex.download(verbose=verbose)
            markers = ex.fetch_all_markers(verbose=verbose)
        counts["extracts"] = ex.import_extracts(
            conn, locale=locale, markers=markers, verbose=verbose
        )
        if do_download:
            counts["extracts"] += ex.import_wiki_only_maps(conn, verbose=verbose)
    conn.close()
    return counts
