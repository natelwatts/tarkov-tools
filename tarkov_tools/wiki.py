"""Open something on the Escape from Tarkov wiki.

Wiki articles are titled by the item's full in-game name, which is exactly
what the item database already stores, so most URLs can be built without
asking anyone anything.

Not everything has an article, though. The template import brings in plenty
of things that only exist inside other things - armour inserts, unnamed
placeholder keys - and guessing a URL for those lands on an empty page that
looks like the tool is broken. So the title is confirmed against the wiki's
API first, and anything it does not recognise falls back to a search, which
is at least a useful place to end up.

The lookup is one small request and it is skipped entirely if the wiki is
slow or unreachable, in which case the guessed URL is used anyway.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

WIKI_BASE = "https://escapefromtarkov.fandom.com/wiki/"
API_URL = "https://escapefromtarkov.fandom.com/api.php"

# Fandom rejects requests that do not look like a browser.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def clean_name(name: str) -> str:
    """The name as the wiki would write it."""
    return (name or "").replace("[DEMO] ", "").strip()


def article_url(title: str) -> str:
    return WIKI_BASE + urllib.parse.quote(clean_name(title).replace(" ", "_"))


def search_url(term: str) -> str:
    query = urllib.parse.urlencode({"search": clean_name(term)})
    return f"{WIKI_BASE}Special:Search?{query}"


def resolve_title(name: str, timeout: float = 3.0) -> str | None:
    """The wiki's own title for this name, or None if it has no article.

    Redirects are followed, so a name the wiki files differently still lands
    on the right article. Raises if the wiki cannot be reached - "no article"
    and "no answer" call for different fallbacks.
    """
    name = clean_name(name)
    if not name:
        return None
    query = urllib.parse.urlencode(
        {"action": "query", "format": "json", "redirects": "1", "titles": name}
    )
    request = urllib.request.Request(
        f"{API_URL}?{query}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    for page in data["query"]["pages"].values():
        if "missing" not in page:
            return page["title"]
    return None


def _words(text: str) -> set[str]:
    """Comparable words, so casing and punctuation do not decide a match."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.lower())
    return {word for word in cleaned.split() if word}


def best_match(name: str, timeout: float = 3.0) -> str | None:
    """The article the wiki files this under when the exact title misses.

    Item names do not always match the wiki's title - it files the M4A1 under
    "Colt M4A1 5.56x45 assault rifle" - and a full-text search finds those.
    But the same search happily returns something loosely related for a thing
    that has no article at all, so a hit is only trusted when one title's
    words are contained in the other's. That accepts a title with a maker's
    name bolted on and rejects "Aramid fiber fabric" for "Aramid insert".
    """
    name = clean_name(name)
    query = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "list": "search",
            "srsearch": name,
            "srlimit": "1",
            "srnamespace": "0",
        }
    )
    request = urllib.request.Request(
        f"{API_URL}?{query}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    hits = data.get("query", {}).get("search") or []
    if not hits:
        return None
    title = hits[0]["title"]
    wanted, found = _words(name), _words(title)
    if wanted <= found or found <= wanted:
        return title
    return None


def page_for(name: str, timeout: float = 3.0) -> tuple[str, str]:
    """(url, title) for this name - its article if there is one, else a search.

    The title comes back too because it is what the browser tab will be
    called, and the wiki's title is not always the name asked for: looking up
    "M4A1 5.56x45 assault rifle" lands on "Colt M4A1 5.56x45 assault rifle".
    Matching a tab on the name asked for would therefore never find it again.

    If the wiki cannot be reached the guessed article URL is used anyway -
    being offline for a moment is no reason to send someone to a search page
    for an item that almost certainly has an article.
    """
    name = clean_name(name)
    try:
        title = resolve_title(name, timeout=timeout) or best_match(name, timeout=timeout)
    except Exception:
        return article_url(name), name
    if title:
        return article_url(title), title
    return search_url(name), name
