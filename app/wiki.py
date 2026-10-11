"""Photos from Wikipedia for places that have none: the article about the place at the same spot (geosearch)."""
import time
from difflib import SequenceMatcher

import httpx

from . import halal
from .config import USER_AGENT

API = "https://en.wikipedia.org/w/api.php"
_cache: dict[str, tuple[float, str]] = {}


def _alike(name: str, title: str) -> bool:
    """The article is about this place: one name contains the other, or they are spelled almost the same."""
    a, b = halal._norm(name), halal._norm(title.split("(")[0])
    return bool(a and b) and (a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.8)


async def photo(name: str, lat: float, lon: float) -> str:
    """A thumbnail URL of the Wikipedia article within 300 m whose title matches the place, or ''."""
    key = f"{name}|{lat:.4f}|{lon:.4f}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    params = {"action": "query", "format": "json", "generator": "geosearch", "ggscoord": f"{lat}|{lon}",
              "ggsradius": 300, "ggslimit": 12, "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 400}
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=6) as c:
        r = await c.get(API, params=params)
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages") or {}
    url = next((p["thumbnail"]["source"] for p in sorted(pages.values(), key=lambda p: p.get("index", 99))
                if p.get("thumbnail", {}).get("source") and _alike(name, p.get("title", ""))
                and not any(w in p["thumbnail"]["source"].lower() for w in ("logo", ".svg", "coat_of_arms", "flag_of"))), "")
    _cache[key] = (time.time(), url)
    return url
