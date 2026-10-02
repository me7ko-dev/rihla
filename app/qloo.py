"""Qloo Taste AI client (hackathon API).

Docs: https://docs.qloo.com/reference/qloo-llm-hackathon-developer-guide
- base URL https://hackathon.api.qloo.com, header X-Api-Key, GET only
- /search (name -> entity), /v2/insights (taste-based recommendations), /v2/tags
Rihla asks Qloo for every kind of stop: sights, halal restaurants and mosques near the destination,
ranked by the traveller's own taste signals (films, books, artists...) plus the "Islam" and
"Parents with young children" audiences. Alcohol-centred venues are excluded on Qloo's side
(filter.exclude.tags) and checked again here.
Without QLOO_API_KEY the client runs in mock mode and answers from fixtures/, so the rest of
the app can be built and tested before the hackathon key arrives.
"""
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

import httpx

from . import config

TYPES = {"place": "urn:entity:place", "movie": "urn:entity:movie", "book": "urn:entity:book", "author": "urn:entity:author",
         "artist": "urn:entity:artist", "album": "urn:entity:album", "tv_show": "urn:entity:tv_show", "brand": "urn:entity:brand",
         "destination": "urn:entity:destination", "podcast": "urn:entity:podcast", "video_game": "urn:entity:videogame",
         "person": "urn:entity:person", "actor": "urn:entity:actor", "director": "urn:entity:director"}
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_cache: dict[str, tuple[float, dict]] = {}


def _cat(*names: str) -> tuple[str, ...]:
    return tuple(f"urn:tag:category:place:{n}" for n in names)


# What the agent can ask for -> Qloo place categories (a place matching any of them qualifies)
CATEGORIES = {
    "museum": _cat("museum", "art_gallery", "science_museum", "children_s_museum"),
    "landmark": _cat("landmark", "historical_landmark", "tourist_attraction", "palace", "castle"),
    "park": _cat("park", "garden"),
    "family": _cat("playground", "aquarium", "zoo", "theme_park", "children_s_museum", "science_museum"),
    "shopping": _cat("market", "shopping_mall", "book_store"),
    "cafe": _cat("cafe", "coffee_shop", "bakery", "ice_cream_shop"),
    "bookstore": _cat("book_store", "library"),
    "stadium": _cat("stadium"),
}
MOSQUE = _cat("mosque")
RESTAURANT = _cat("restaurant")
HALAL_CUISINE = "urn:tag:cuisine:qloo:halal"
# The place itself is listed as a halal restaurant (vs. diners merely mentioning halal in reviews)
HALAL_LISTED = {"urn:tag:category:place:halal_restaurant", "urn:tag:genre:place:restaurant:halal"}
NO_ALCOHOL = _cat("bar", "pub", "night_club", "wine_bar", "brewery", "cocktail_bar", "beer_garden", "liquor_store",
                  "casino", "hookah_bar") + tuple(f"urn:tag:genre:place:{g}" for g in (
                  "pub", "restaurant:bar", "restaurant:pub", "restaurant:wine_bar", "restaurant:cocktail_bar", "restaurant:brewery")) + (
                  "urn:tag:cuisine:qloo:meyhane",)
# The place serves alcohol (many restaurants do): shown to the traveller and ranked lower, but not excluded
SERVES_ALCOHOL = {"urn:tag:offerings:place:alcohol", "urn:tag:offerings:place:hard_liquor", "urn:tag:offerings:place:cocktails",
                  "urn:tag:beverage_offering:qloo:full_bar", "urn:tag:beverage_offering:qloo:spirits", "urn:tag:amenity:qloo:full_bar"}
KIDS_TAG = "urn:tag:children:place:good_kids"
AUDIENCES = {"muslim": "urn:audience:lifestyle_preferences_beliefs:islam",
             "kids": "urn:audience:life_stage:parents_with_young_children"}
_ALCOHOL_NAME = re.compile(r"\b(bar|pub|brewery|brewpub|taproom|tavern|winery|wine|cocktails?|nightclub|night club|"
                           r"beer|liquor|saloon|casino|meyhane|izakaya|biergarten)\b", re.I)


class QlooError(RuntimeError):
    pass


# The hackathon key allows 5 requests per second and 10,000 per month: responses are cached on disk for a
# week (the same city and tastes cost nothing the second time) and requests are spaced to stay under the limit.
CACHE_DIR = config.CACHE / "qloo"
DISK_TTL = 7 * 86400
PER_SECOND = 4
_sent: list[float] = []
_lock: asyncio.Lock | None = None
_inflight: dict[str, asyncio.Future] = {}
stats = {"requests": 0, "cached": 0}


def _disk(key: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".json")


async def _turn() -> None:
    """Wait until fewer than PER_SECOND requests went out in the last second."""
    global _lock
    _lock = _lock or asyncio.Lock()
    async with _lock:
        while True:
            now = time.monotonic()
            _sent[:] = [t for t in _sent if now - t < 1.0]
            if len(_sent) < PER_SECOND:
                _sent.append(now)
                return
            await asyncio.sleep(1.0 - (now - _sent[0]) + 0.01)


async def _get(path: str, params: dict) -> dict:
    key = path + "?" + json.dumps(params, sort_keys=True)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    if config.QLOO_MOCK:
        return _mock(path, params)
    f = _disk(key)
    if f.exists() and time.time() - f.stat().st_mtime < DISK_TTL:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            _cache[key] = (time.time(), data)
            stats["cached"] += 1
            return data
        except (OSError, ValueError):
            pass
    if key in _inflight:  # the same request is already on its way
        return await asyncio.shield(_inflight[key])
    fut = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    try:
        async with httpx.AsyncClient(base_url=config.QLOO_BASE_URL, timeout=30,
                                     headers={"X-Api-Key": config.QLOO_API_KEY, "Accept": "application/json"}) as c:
            for attempt in range(5):
                await _turn()
                r = await c.get(path, params=params)
                stats["requests"] += 1
                if r.status_code != 429:
                    break
                await asyncio.sleep(float(r.headers.get("x-second-ratelimit-reset") or 0.5) + 0.2 * attempt)
        if r.status_code != 200:
            raise QlooError(f"Qloo {path} -> HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        _cache[key] = (time.time(), data)
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass
        fut.set_result(data)
        return data
    except Exception as e:
        fut.set_exception(e)
        fut.exception()  # mark as retrieved: waiters get it via shield, no "never retrieved" warning
        raise
    finally:
        _inflight.pop(key, None)


def _entities(data: dict) -> list[dict]:
    res = data.get("results", data)
    if isinstance(res, dict):
        res = res.get("entities") or res.get("tags") or []
    return res if isinstance(res, list) else []


def _simplify(e: dict) -> dict:
    p = e.get("properties") or {}
    loc = e.get("location") or p.get("location") or p.get("geocode") or {}
    # /v2/insights names it "id", /search "tag_id"
    tag_ids = [t.get("id") or t.get("tag_id") for t in (e.get("tags") or []) if isinstance(t, dict) and (t.get("id") or t.get("tag_id"))]
    imgs, img = p.get("images") or [], p.get("image")
    if imgs and isinstance(imgs[0], dict):
        img = imgs[0].get("url")
    elif isinstance(img, dict):
        img = img.get("url")
    q = e.get("query") or {}
    kw = sorted((k for k in p.get("keywords") or [] if isinstance(k, dict) and k.get("name")), key=lambda k: -(k.get("count") or 0))
    kids = next((g.get("weight") for g in p.get("good_for") or [] if isinstance(g, dict) and g.get("id") == KIDS_TAG), None)
    return {
        "id": e.get("entity_id") or e.get("id"),
        "name": e.get("name", ""),
        "type": e.get("subtype") or (e.get("types") or [e.get("type")])[0],
        "lat": loc.get("lat") or loc.get("latitude"),
        "lon": loc.get("lon") or loc.get("lng") or loc.get("longitude"),
        "address": p.get("address", ""),
        "description": (p.get("short_description") or p.get("description") or "")[:300],
        "known_for": [k["name"] for k in kw[:8]],  # what reviewers mention most, e.g. "dinosaurs" at the Natural History Museum
        "kids": kids,
        "image": img,
        "website": p.get("website") or "",
        "price_level": p.get("price_level"),
        "rating": p.get("business_rating") or p.get("rating"),
        "tag_ids": tag_ids,
        "categories": [t.rsplit(":", 1)[1].replace("_", " ") for t in tag_ids if t.startswith("urn:tag:category:place:")][:4],
        "affinity": q.get("affinity") or e.get("affinity"),
        "popularity": e.get("popularity"),
    }


def is_alcohol_venue(item: dict) -> bool:
    return bool(set(item.get("tag_ids") or ()) & set(NO_ALCOHOL)) or bool(_ALCOHOL_NAME.search(item.get("name", "")))


def serves_alcohol(item: dict) -> bool:
    return bool(set(item.get("tag_ids") or ()) & SERVES_ALCOHOL)


def halal_signal(item: dict) -> str:
    """'listed' (a halal restaurant), 'mentioned' (halal in its tags/reviews) or ''."""
    ids = set(item.get("tag_ids") or ())
    if ids & HALAL_LISTED:
        return "listed"
    return "mentioned" if any("halal" in t for t in ids) else ""


async def search(query: str, kind: str | None = None, take: int = 3) -> list[dict]:
    """Free text -> Qloo entities (to turn 'Pixar films' or 'Orhan Pamuk' into taste signals)."""
    params = {"query": query, "take": take}
    if kind:
        params["types"] = TYPES.get(kind, kind)
    return [_simplify(e) for e in _entities(await _get("/search", params))]


async def insights(kind: str, *, interests: list[str] | None = None, audiences: list[str] | None = None,
                   lat: float | None = None, lon: float | None = None, radius_m: int = 4000,
                   tags: tuple | list | None = None, exclude_tags: tuple | list | None = None, take: int = 20) -> list[dict]:
    """Taste-based recommendations of one entity type, optionally around a point.
    Places never include alcohol-centred venues."""
    params: dict = {"filter.type": TYPES.get(kind, kind), "take": min(take, 50)}
    if interests:
        params["signal.interests.entities"] = ",".join(interests[:10])
    if audiences:
        params["signal.demographics.audiences"] = ",".join(audiences)
    if lat is not None and lon is not None:
        params["filter.location"] = f"POINT({lon} {lat})"
        params["filter.location.radius"] = radius_m
    if tags:
        params["filter.tags"] = ",".join(tags)
    if kind == "place":
        exclude_tags = tuple(exclude_tags or ()) + NO_ALCOHOL
    if exclude_tags:
        params["filter.exclude.tags"] = ",".join(dict.fromkeys(exclude_tags))
    items = [_simplify(e) for e in _entities(await _get("/v2/insights", params))]
    return [i for i in items if i.get("id") and not (kind == "place" and is_alcohol_venue(i))]


async def find_tags(query: str, take: int = 5) -> list[dict]:
    data = await _get("/v2/tags", {"filter.query": query, "feature.semantic_search": "true", "take": take})
    return [{"id": t.get("id") or t.get("tag_id"), "name": t.get("name"), "type": t.get("type") or t.get("subtype")}
            for t in _entities(data)]


# ---------- mock mode (no key) ----------
def _mock(path: str, params: dict) -> dict:
    if path == "/search":
        q = params.get("query", "")
        return {"results": [{"entity_id": f"MOCK-{abs(hash(q)) % 10**8:08d}", "name": q.title(),
                             "types": [params.get("types", "urn:entity:movie")], "properties": {}}]}
    if path == "/v2/tags":
        return {"results": {"tags": [{"id": "urn:tag:mock:" + params.get("filter.query", "x").replace(" ", "_"),
                                      "name": params.get("filter.query", "")}]}}
    f = FIXTURES / "insights_place.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"results": {"entities": []}}
