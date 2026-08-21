"""Minimal GraphQL client for the public tarkov.dev API.

No API key exists and none is needed - the endpoint is open. If you see
HTTP 422 with {"errors": ["GraphQL server unavailable. Try again later."]}
that is their Cloudflare Worker failing to reach its data backend, i.e. an
outage on their side, not an auth problem. Auth failures would be 401/403.

Raw responses are cached under data/raw/ so the database can be rebuilt
without re-fetching, and so development can continue during an outage.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import DATA_DIR, load_config

USER_AGENT = "tarkov-tools/0.1 (personal, non-commercial; stdlib urllib)"
RAW_DIR = DATA_DIR / "raw"


class ApiUnavailable(RuntimeError):
    """The API is reachable but reporting itself as unavailable."""


class GraphQLError(RuntimeError):
    """The API returned GraphQL-level errors for a valid request."""


class TarkovDevClient:
    def __init__(
        self,
        endpoint: str | None = None,
        timeout: int | None = None,
        max_retries: int | None = None,
    ):
        cfg = load_config()["api"]
        self.endpoint = endpoint or cfg["endpoint"]
        self.timeout = timeout or cfg["timeout_seconds"]
        self.max_retries = max_retries if max_retries is not None else cfg["max_retries"]
        self.language = cfg["language"]
        self.game_mode = cfg["game_mode"]

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))

    def query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a query, retrying with exponential backoff on outages."""
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        delay = 2.0
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                result = self._post(payload)
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                # 422 with their "unavailable" string means a backend outage.
                if exc.code in (422, 500, 502, 503, 504):
                    last_error = ApiUnavailable(
                        f"HTTP {exc.code} from tarkov.dev: {detail or exc.reason}"
                    )
                else:
                    raise RuntimeError(f"HTTP {exc.code} from tarkov.dev: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            else:
                if result.get("errors"):
                    messages = [
                        e.get("message", str(e)) if isinstance(e, dict) else str(e)
                        for e in result["errors"]
                    ]
                    joined = "; ".join(messages)
                    if "unavailable" in joined.lower():
                        last_error = ApiUnavailable(joined)
                    else:
                        raise GraphQLError(joined)
                else:
                    return result["data"]

            if attempt < self.max_retries:
                print(f"  attempt {attempt}/{self.max_retries} failed ({last_error}); "
                      f"retrying in {delay:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, 60.0)

        raise ApiUnavailable(
            f"tarkov.dev did not respond successfully after {self.max_retries} attempts. "
            f"Last error: {last_error}\n"
            "This endpoint needs no API key - it is an outage on their side. "
            "Check https://status.tarkov.dev or try again later."
        )

    # --- cached fetch helpers ------------------------------------------

    def fetch(self, name: str, query: str, use_cache: bool = False) -> dict[str, Any]:
        """Run a named query, optionally serving from the on-disk cache."""
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = RAW_DIR / f"{name}.json"

        if use_cache and cache_path.exists():
            print(f"  [cache] {name}")
            return json.loads(cache_path.read_text(encoding="utf-8"))

        variables = {"lang": self.language, "mode": self.game_mode}
        data = self.query(query, variables)
        cache_path.write_text(json.dumps(data), encoding="utf-8")
        return data


# --- queries -----------------------------------------------------------

PRICE_FRAGMENT = """
  buyFor {
    price
    currency
    priceRUB
    vendor {
      name
      normalizedName
      ... on TraderOffer { minTraderLevel }
    }
  }
"""

ITEM_FIELDS = """
  id
  name
  shortName
  normalizedName
  basePrice
  avg24hPrice
  low24hPrice
  lastLowPrice
  wikiLink
  iconLink
  width
  height
  weight
  types
"""

AMMO_QUERY = """
query Ammo($lang: LanguageCode, $mode: GameMode) {
  ammo(lang: $lang, gameMode: $mode) {
    caliber
    damage
    armorDamage
    fragmentationChance
    ricochetChance
    penetrationChance
    penetrationPower
    penetrationPowerDeviation
    accuracyModifier
    recoilModifier
    initialSpeed
    lightBleedModifier
    heavyBleedModifier
    stackMaxSize
    tracer
    ammoType
    projectileCount
    item { %s %s }
  }
}
""" % (ITEM_FIELDS, PRICE_FRAGMENT)

GUNS_QUERY = """
query Guns($lang: LanguageCode, $mode: GameMode) {
  items(types: [gun], lang: $lang, gameMode: $mode) {
    %s
    %s
    properties {
      __typename
      ... on ItemPropertiesWeapon {
        caliber
        ergonomics
        recoilVertical
        recoilHorizontal
        fireRate
        fireModes
        effectiveDistance
        sightingRange
        defaultAmmo { id }
        defaultPreset { id }
        allowedAmmo { id }
        slots {
          name
          nameId
          required
          filters { allowedItems { id } }
        }
      }
    }
  }
}
""" % (ITEM_FIELDS, PRICE_FRAGMENT)

MODS_QUERY = """
query Mods($lang: LanguageCode, $mode: GameMode) {
  items(types: [mods], lang: $lang, gameMode: $mode) {
    %s
    %s
    properties {
      __typename
      ... on ItemPropertiesMagazine {
        capacity
        ergonomics
        recoilModifier
        loadModifier
        ammoCheckModifier
        malfunctionChance
        allowedAmmo { id }
      }
    }
  }
}
""" % (ITEM_FIELDS, PRICE_FRAGMENT)
