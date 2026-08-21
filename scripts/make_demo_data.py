"""Generate a small DEMO dataset in the exact shape the tarkov.dev API returns.

This exists so the ingest / search / popover code can be exercised end to end
while the upstream API is unavailable.

The ballistic numbers here are INVENTED. Every item name is prefixed with
[DEMO] so demo data can never be mistaken for real game stats. Run a real
`sync` to replace it all with authoritative values from tarkov.dev.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tarkov_tools.config import DATA_DIR  # noqa: E402

RAW = DATA_DIR / "raw"


def item(iid, name, short, types, price=1000, weight=0.1, w=1, h=1, offers=()):
    return {
        "id": iid,
        "name": f"[DEMO] {name}",
        "shortName": short,
        "normalizedName": name.lower().replace(" ", "-").replace(".", ""),
        "basePrice": price,
        "avg24hPrice": int(price * 1.4),
        "low24hPrice": int(price * 1.1),
        "lastLowPrice": int(price * 1.15),
        "wikiLink": "https://escapefromtarkov.fandom.com/",
        "iconLink": "",
        "width": w,
        "height": h,
        "weight": weight,
        "types": types,
        "buyFor": [
            {
                "price": p,
                "currency": "RUB",
                "priceRUB": p,
                "vendor": {"name": v, "normalizedName": v.lower(), "minTraderLevel": lvl},
            }
            for v, lvl, p in offers
        ],
    }


# --- calibers, ammo -----------------------------------------------------

AMMO_SPEC = [
    # (id, name, short, caliber, damage, pen, armor_dmg, frag, speed, price, offers)
    ("a1", "5.56x45mm Alpha", "ALPHA", "Caliber556x45NATO", 40, 53, 60, 0.10, 1013, 900,
     [("Peacekeeper", 4, 900)]),
    ("a2", "5.56x45mm Bravo", "BRAVO", "Caliber556x45NATO", 43, 37, 52, 0.15, 950, 350,
     [("Peacekeeper", 2, 350), ("Prapor", 3, 400)]),
    ("a3", "5.56x45mm Charlie", "CHARLIE", "Caliber556x45NATO", 57, 22, 40, 0.35, 880, 120,
     [("Peacekeeper", 1, 120)]),
    ("a4", "5.45x39mm Delta", "DELTA", "Caliber545x39", 40, 51, 58, 0.12, 880, 850,
     [("Prapor", 4, 850)]),
    ("a5", "5.45x39mm Echo", "ECHO", "Caliber545x39", 44, 37, 50, 0.16, 890, 300,
     [("Prapor", 3, 300)]),
    ("a6", "5.45x39mm Foxtrot", "FOX", "Caliber545x39", 46, 29, 42, 0.20, 915, 90,
     [("Prapor", 1, 90)]),
    ("a7", "7.62x39mm Golf", "GOLF", "Caliber762x39", 58, 47, 63, 0.18, 730, 700,
     [("Prapor", 3, 700), ("Jaeger", 2, 720)]),
    ("a8", "7.62x39mm Hotel", "HOTEL", "Caliber762x39", 57, 33, 48, 0.19, 716, 180,
     [("Prapor", 2, 180)]),
    ("a9", "9x19mm India", "INDIA", "Caliber9x19PARA", 52, 34, 40, 0.08, 480, 250,
     [("Peacekeeper", 2, 250)]),
    ("a10", "9x19mm Juliet", "JULIET", "Caliber9x19PARA", 102, 2, 6, 0.00, 400, 60,
     [("Therapist", 1, 60)]),
]

AMMO = [
    {
        "caliber": cal,
        "damage": dmg,
        "armorDamage": ad,
        "fragmentationChance": frag,
        "ricochetChance": 0.3,
        "penetrationChance": 0.4,
        "penetrationPower": pen,
        "penetrationPowerDeviation": 0.5,
        "accuracyModifier": 0.0,
        "recoilModifier": 0.0,
        "initialSpeed": speed,
        "lightBleedModifier": 0.0,
        "heavyBleedModifier": 0.0,
        "stackMaxSize": 60,
        "tracer": False,
        "ammoType": "bullet",
        "projectileCount": 1,
        "item": item(iid, name, short, ["ammo"], price, 0.01, offers=offers),
    }
    for (iid, name, short, cal, dmg, pen, ad, frag, speed, price, offers) in AMMO_SPEC
]

AMMO_BY_CAL: dict[str, list[str]] = {}
for spec in AMMO_SPEC:
    AMMO_BY_CAL.setdefault(spec[3], []).append(spec[0])


# --- magazines ----------------------------------------------------------

MAG_SPEC = [
    # (id, name, short, caliber, capacity, ergo, price, offers)
    ("m1", "STANAG 30-round", "STANAG", "Caliber556x45NATO", 30, -3, 2000,
     [("Peacekeeper", 1, 2000)]),
    ("m2", "STANAG 60-round", "SG60", "Caliber556x45NATO", 60, -12, 12000,
     [("Peacekeeper", 3, 12000)]),
    ("m3", "AK-74 30-round", "AK30", "Caliber545x39", 30, -2, 1800,
     [("Prapor", 1, 1800)]),
    ("m4", "AK-74 60-round", "AK60", "Caliber545x39", 60, -11, 9000,
     [("Prapor", 3, 9000)]),
    ("m5", "AKM 30-round", "AKM30", "Caliber762x39", 30, -3, 2200,
     [("Prapor", 2, 2200)]),
    ("m6", "MP5 30-round", "MP530", "Caliber9x19PARA", 30, -2, 1500,
     [("Peacekeeper", 1, 1500)]),
]

MAGS = [
    {
        **item(mid, name, short, ["mods"], price, 0.15, 1, 2, offers=offers),
        "properties": {
            "__typename": "ItemPropertiesMagazine",
            "capacity": cap,
            "ergonomics": ergo,
            "recoilModifier": 0.0,
            "loadModifier": 0.0,
            "ammoCheckModifier": 0.0,
            "malfunctionChance": 0.01,
            "allowedAmmo": [{"id": a} for a in AMMO_BY_CAL.get(cal, [])],
        },
    }
    for (mid, name, short, cal, cap, ergo, price, offers) in MAG_SPEC
]

MAGS_BY_CAL: dict[str, list[str]] = {}
for spec in MAG_SPEC:
    MAGS_BY_CAL.setdefault(spec[3], []).append(spec[0])


# --- guns ---------------------------------------------------------------

GUN_SPEC = [
    # (id, name, short, caliber, ergo, rec_v, rec_h, rpm, price, offers)
    ("g1", "Assault Rifle Alpha", "AR-A", "Caliber556x45NATO", 52, 62, 254, 800, 35000,
     [("Peacekeeper", 2, 35000)]),
    ("g2", "Carbine Bravo", "CB-B", "Caliber556x45NATO", 44, 70, 280, 700, 28000,
     [("Peacekeeper", 1, 28000)]),
    ("g3", "Assault Rifle Charlie", "AR-C", "Caliber545x39", 44, 74, 269, 650, 24000,
     [("Prapor", 2, 24000)]),
    ("g4", "Assault Rifle Delta", "AR-D", "Caliber762x39", 43, 96, 342, 600, 26000,
     [("Prapor", 2, 26000), ("Jaeger", 3, 25500)]),
    ("g5", "Submachine Gun Echo", "SMG-E", "Caliber9x19PARA", 62, 65, 190, 800, 18000,
     [("Peacekeeper", 1, 18000)]),
]

GUNS = [
    {
        **item(gid, name, short, ["gun"], price, 3.0, 4, 2, offers=offers),
        "properties": {
            "__typename": "ItemPropertiesWeapon",
            "caliber": cal,
            "ergonomics": ergo,
            "recoilVertical": rv,
            "recoilHorizontal": rh,
            "fireRate": rpm,
            "fireModes": ["Single", "Full auto"],
            "effectiveDistance": 500,
            "sightingRange": 800,
            "defaultAmmo": {"id": AMMO_BY_CAL.get(cal, ["a1"])[-1]},
            "defaultPreset": None,
            "allowedAmmo": [{"id": a} for a in AMMO_BY_CAL.get(cal, [])],
            "slots": [
                {
                    "name": "mod_magazine",
                    "nameId": "mod_magazine",
                    "required": True,
                    "filters": {
                        "allowedItems": [{"id": m} for m in MAGS_BY_CAL.get(cal, [])]
                    },
                },
                {
                    "name": "mod_muzzle",
                    "nameId": "mod_muzzle",
                    "required": False,
                    "filters": {"allowedItems": []},
                },
            ],
        },
    }
    for (gid, name, short, cal, ergo, rv, rh, rpm, price, offers) in GUN_SPEC
]


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / "ammo.json").write_text(json.dumps({"ammo": AMMO}), encoding="utf-8")
    (RAW / "guns.json").write_text(json.dumps({"items": GUNS}), encoding="utf-8")
    (RAW / "mods.json").write_text(json.dumps({"items": MAGS}), encoding="utf-8")
    print(f"wrote demo fixtures to {RAW}")
    print(f"  ammo {len(AMMO)}  guns {len(GUNS)}  magazines {len(MAGS)}")
    print("\nAll names are prefixed [DEMO] and all stats are invented.")
    print("Run a real sync to replace them with authoritative tarkov.dev data.")


if __name__ == "__main__":
    main()
