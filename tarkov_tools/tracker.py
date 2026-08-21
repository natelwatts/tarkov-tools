"""TarkovTracker account integration.

Reads your quest and hideout progress so the popover can answer "do I still
need this?". Progress itself is only ids and completion flags, so what is
still needed gets computed by joining it against the task and hideout
definitions, which TarkovTracker serves publicly with no auth.

The API token is a credential for your account. It is stored in
config.local.json, which is gitignored, and never printed back in full.
Read-only ("GP") permission is all this needs; nothing here writes to your
account.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import LOCAL_CONFIG_PATH, load_config, set_local_override

GATEWAY = "https://api.tarkovtracker.org"
PUBLIC_DATA = "https://tarkovtracker.org/api/tarkov"
USER_AGENT = "tarkov-tools/0.1 (personal, non-commercial)"

# The prefix fixes which game mode a token reads; a mismatch is rejected 401.
TOKEN_PREFIXES = ("PVP_", "PVE_", "SZN_")


class TrackerError(RuntimeError):
    pass


def mask(token: str) -> str:
    """Show enough to recognise a token, never enough to use it."""
    if not token:
        return "(none)"
    prefix = next((p for p in TOKEN_PREFIXES if token.startswith(p)), "")
    tail = token[-4:] if len(token) > 8 else ""
    return f"{prefix}...{tail}"


def load_token() -> str | None:
    return (load_config().get("tracker") or {}).get("token") or None


def save_token(token: str) -> None:
    set_local_override("tracker", "token", token)


def _request(url: str, token: str | None = None, timeout: int = 45) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            limit = response.headers.get("X-RateLimit-Remaining")
            if limit is not None and isinstance(payload, dict):
                payload.setdefault("_rate_remaining", limit)
            return payload
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if exc.code == 401:
            raise TrackerError(
                "Rejected (401). Either the token is wrong, or its game-mode "
                "prefix does not match the data being requested - a PVE_ token "
                "only reads PVE progress, and a PVP_ token only PVP."
            ) from exc
        if exc.code == 429:
            retry = exc.headers.get("Retry-After", "?")
            raise TrackerError(f"Rate limited (429). Retry after {retry}s.") from exc
        raise TrackerError(f"HTTP {exc.code} from TarkovTracker: {body}") from exc
    except urllib.error.URLError as exc:
        raise TrackerError(f"Could not reach TarkovTracker: {exc.reason}") from exc


# --- account ------------------------------------------------------------

def token_info(token: str) -> dict:
    """Verify a token and report what it can do."""
    data = _request(f"{GATEWAY}/token", token)
    if not isinstance(data, dict) or not data.get("success"):
        raise TrackerError(f"Unexpected reply: {str(data)[:200]}")
    return data


def get_progress(token: str) -> dict:
    data = _request(f"{GATEWAY}/progress", token)
    if not isinstance(data, dict) or not data.get("success"):
        raise TrackerError(f"Unexpected reply: {str(data)[:200]}")
    return data.get("data") or {}


# --- public game data (no token) ---------------------------------------

def fetch_public(name: str, game_mode: str | None = None) -> Any:
    """One of the public definition feeds: tasks-core, tasks-objectives, hideout.

    These need no authentication at all.
    """
    url = f"{PUBLIC_DATA}/{name}"
    if game_mode:
        url += f"?gameMode={game_mode}"
    payload = _request(url, timeout=120)
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


# --- needed items -------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS needed_items (
    item_id      TEXT NOT NULL,
    source_kind  TEXT NOT NULL,       -- 'task' or 'hideout'
    source_id    TEXT NOT NULL,
    source_name  TEXT,
    detail_id    TEXT NOT NULL,       -- objective id, or requirement id
    need         INTEGER,
    have         INTEGER,
    found_in_raid INTEGER,
    optional     INTEGER,
    alternatives INTEGER,             -- how many items satisfy this one need
    available    INTEGER,             -- prerequisites and level already met
    wiki_link    TEXT,
    PRIMARY KEY (item_id, source_kind, source_id, detail_id)
);
CREATE INDEX IF NOT EXISTS idx_needed_item ON needed_items(item_id);
"""

# Objective types that actually consume items you must go and find.
ITEM_OBJECTIVES = ("giveItem", "findItem", "plantItem")

# Money is technically an item requirement, but "you need 18,222,000 Roubles"
# is not a shopping list and would swamp everything else.
CURRENCY_IDS = {
    "5449016a4bdc2d6f028b456f",  # Roubles
    "5696686a4bdc2da3298b456a",  # Dollars
    "569668774bdc2da2298b4568",  # Euros
    "5d235b4d86f7742e017bc88a",  # GP coin
}


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def sync_needed(conn, token: str | None = None, game_mode: str | None = None,
                verbose: bool = True, on_status=None) -> dict[str, int]:
    """Work out what you still need, and store it.

    Progress carries only ids and completion flags, so the outstanding items
    come from joining it against the public task and hideout definitions.

    on_status, if given, is called with a short progress line at each stage -
    the definition download is a few megabytes, so a caller with a UI needs
    something to show meanwhile.
    """
    def status(message: str) -> None:
        if verbose:
            print(message)
        if on_status:
            on_status(message)

    token = token or load_token()
    if not token:
        raise TrackerError("No TarkovTracker token saved. Run 'tracker login' first.")

    ensure_schema(conn)
    status("fetching your progress ...")
    progress = get_progress(token)
    level = progress.get("playerLevel") or 0
    faction = (progress.get("pmcFaction") or "").upper()

    done_tasks = {t["id"] for t in progress.get("tasksProgress") or [] if t.get("complete")}
    dead_tasks = {t["id"] for t in progress.get("tasksProgress") or []
                  if t.get("failed") or t.get("invalid")}
    objective_state = {
        o["id"]: o for o in progress.get("taskObjectivesProgress") or []
    }
    done_modules = {m["id"] for m in progress.get("hideoutModulesProgress") or []
                    if m.get("complete")}
    part_state = {p["id"]: p for p in progress.get("hideoutPartsProgress") or []}

    status(f"  {progress.get('displayName')} - level {level}, {faction}")
    status("fetching task and hideout definitions ...")
    core = fetch_public("tasks-core", game_mode)
    objectives = fetch_public("tasks-objectives", game_mode)
    hideout = fetch_public("hideout", game_mode)

    tasks = {t["id"]: t for t in core.get("tasks") or []}
    objectives_by_task = {t["id"]: t.get("objectives") or []
                          for t in objectives.get("tasks") or []}

    conn.execute("DELETE FROM needed_items")
    task_rows = hideout_rows = 0

    for task_id, task in tasks.items():
        if task_id in done_tasks or task_id in dead_tasks:
            continue
        task_faction = (task.get("factionName") or "Any").upper()
        if task_faction not in ("ANY", faction):
            continue  # the other faction's version of a task

        # "Available" means you could start it now: level is high enough and
        # every prerequisite task is already complete.
        available = (task.get("minPlayerLevel") or 0) <= level
        for requirement in task.get("taskRequirements") or []:
            required = (requirement.get("task") or {}).get("id")
            statuses = requirement.get("status") or []
            if "complete" in statuses and required not in done_tasks:
                available = False
                break

        for objective in objectives_by_task.get(task_id, []):
            if objective.get("type") not in ITEM_OBJECTIVES:
                continue
            state = objective_state.get(objective.get("id")) or {}
            if state.get("complete"):
                continue
            items = objective.get("items") or []
            if not items:
                continue
            need = objective.get("count") or 1
            have = state.get("count") or 0
            if need - have <= 0:
                continue  # nothing outstanding even though it is not ticked
            for entry in items:
                if entry.get("id") in CURRENCY_IDS:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO needed_items
                    (item_id, source_kind, source_id, source_name, detail_id,
                     need, have, found_in_raid, optional, alternatives,
                     available, wiki_link)
                    VALUES (?,'task',?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        entry.get("id"), task_id, task.get("name"),
                        objective.get("id"), need, have,
                        int(bool(objective.get("foundInRaid"))),
                        int(bool(objective.get("optional"))),
                        len(items), int(available), task.get("wikiLink"),
                    ),
                )
                task_rows += 1

    for station in hideout.get("hideoutStations") or []:
        for level_entry in station.get("levels") or []:
            if level_entry.get("id") in done_modules:
                continue
            name = f"{station.get('name')} level {level_entry.get('level')}"
            for requirement in level_entry.get("itemRequirements") or []:
                item = (requirement.get("item") or {}).get("id")
                if not item:
                    continue
                state = part_state.get(requirement.get("id")) or {}
                if state.get("complete") or item in CURRENCY_IDS:
                    continue
                required_count = requirement.get("count") or requirement.get("quantity") or 1
                if required_count - (state.get("count") or 0) <= 0:
                    continue
                fir = any(
                    a.get("name") == "foundInRaid" and str(a.get("value")).lower() == "true"
                    for a in requirement.get("attributes") or []
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO needed_items
                    (item_id, source_kind, source_id, source_name, detail_id,
                     need, have, found_in_raid, optional, alternatives,
                     available, wiki_link)
                    VALUES (?,'hideout',?,?,?,?,?,?,0,1,1,NULL)
                    """,
                    (
                        item, level_entry.get("id"), name, requirement.get("id"),
                        requirement.get("count") or requirement.get("quantity") or 1,
                        state.get("count") or 0, int(fir),
                    ),
                )
                hideout_rows += 1

    # The template mirror lags the live game by a patch or two, so some needed
    # items have no local name and would show as a raw id. Fill just those in
    # from the public item feed.
    missing = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT n.item_id FROM needed_items n"
            " LEFT JOIN items i ON i.id = n.item_id WHERE i.id IS NULL"
        )
    ]
    if missing:
        status(f"  filling in {len(missing)} item names missing locally ...")
        lite = fetch_public("items-lite", game_mode)
        by_id = {i["id"]: i for i in (lite.get("items") or [])}
        filled = 0
        for item_id in missing:
            info = by_id.get(item_id)
            if not info:
                continue
            conn.execute(
                """
                INSERT INTO items (id, name, short_name, normalized_name, types, wiki_link)
                VALUES (?,?,?,?,'["item"]',?)
                ON CONFLICT(id) DO UPDATE SET
                    name = COALESCE(items.name, excluded.name),
                    short_name = COALESCE(items.short_name, excluded.short_name)
                """,
                (item_id, info.get("name"), info.get("shortName"),
                 info.get("normalizedName"), info.get("wikiLink")),
            )
            filled += 1
        status(f"  filled {filled}")
        from . import db as dbmod

        dbmod.rebuild_fts(conn)

    distinct = conn.execute(
        "SELECT COUNT(DISTINCT item_id) FROM needed_items"
    ).fetchone()[0]
    status(f"  {task_rows} task requirements, {hideout_rows} hideout requirements")
    status(f"  {distinct} distinct items still needed")
    return {"tasks": task_rows, "hideout": hideout_rows, "items": distinct}


def needs_for_item(conn, item_id: str, available_only: bool = False) -> list[dict]:
    """Everything still asking for this item, or nothing if never synced.

    Deliberately does not create the table: a database that has never seen a
    sync should stay that way, so the optional integration leaves no trace.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='needed_items'"
    ).fetchone()
    if not row:
        return []
    clause = " AND available = 1" if available_only else ""
    rows = conn.execute(
        # Unlocked first, then tasks before hideout: a quest you can hand in
        # now matters more than a station upgrade you will get to eventually.
        f"SELECT * FROM needed_items WHERE item_id = ? AND need > have{clause}"
        " ORDER BY available DESC, source_kind DESC, source_name",
        (item_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def describe_token(token: str) -> str:
    info = token_info(token)
    permissions = info.get("permissions") or []
    readable = {"GP": "read progress", "WP": "write progress"}
    perms = ", ".join(readable.get(p, p) for p in permissions)
    return (
        f"  token       {mask(info.get('token') or token)}\n"
        f"  game mode   {info.get('gameMode')}\n"
        f"  permissions {perms or '(none)'}\n"
        f"  note        {info.get('note') or '-'}\n"
        f"  calls used  {info.get('calls')}"
    )


def config_path_hint() -> str:
    return str(LOCAL_CONFIG_PATH)
