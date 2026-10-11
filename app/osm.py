"""OpenStreetMap: geocoding (Nominatim), halal food and mosques (Overpass)."""
import asyncio
import json
import math
import re
import time

import httpx

from .config import CACHE, SEED, USER_AGENT

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
_HEADERS = {"User-Agent": USER_AGENT}
_cache: dict[str, tuple[float, object]] = {}
_TTL = 6 * 3600
_nominatim_lock = asyncio.Lock()
_last_nominatim = 0.0


def _cached(key: str):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    return None


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def geocode(place: str) -> dict | None:
    """Place name -> {name, lat, lon, country_code}. Nominatim allows 1 request per second."""
    global _last_nominatim
    key = "geo:" + place.lower().strip()
    if (hit := _cached(key)) is not None:
        return hit
    async with _nominatim_lock:
        wait = 1.05 - (time.time() - _last_nominatim)
        if wait > 0:
            await asyncio.sleep(wait)
        async with httpx.AsyncClient(headers=_HEADERS, timeout=20) as c:
            r = await c.get(NOMINATIM, params={"q": place, "format": "jsonv2", "limit": 5, "addressdetails": 1,
                                               "accept-language": "en"})
        _last_nominatim = time.time()
    r.raise_for_status()
    x = best_match(r.json())
    out = None
    if x:
        out = {"name": x.get("display_name", place), "lat": float(x["lat"]), "lon": float(x["lon"]),
               "country_code": (x.get("address") or {}).get("country_code", "")}
    _cache[key] = (time.time(), out)
    return out


SETTLEMENTS = ("city", "town", "village", "suburb", "quarter", "neighbourhood", "city_district", "borough")


def best_match(rows: list[dict]) -> dict | None:
    """The result people mean: the most important one ("Mecca" is Makkah, not a hamlet in Italy); among equally
    important ones the town itself rather than its administrative area ("Dubai" is the city centre, not the middle
    of the emirate's boundary 13 km away)."""
    if not rows:
        return None
    top = max(float(x.get("importance") or 0) for x in rows)
    tied = [x for x in rows if float(x.get("importance") or 0) >= top - 0.005]
    return min(tied, key=lambda x: (x.get("category") != "place" or x.get("type") not in SETTLEMENTS,
                                    x.get("addresstype") not in SETTLEMENTS[:3]))


async def _overpass(query: str) -> list[dict]:
    """The public Overpass servers allow ~2 parallel queries per IP and often time out: retry with backoff."""
    last: Exception | None = None
    for attempt in range(3):
        for url in OVERPASS:
            try:
                async with httpx.AsyncClient(headers=_HEADERS, timeout=httpx.Timeout(100, connect=15)) as c:
                    r = await c.post(url, data={"data": query})
                if r.status_code == 200:
                    return r.json().get("elements", [])
                last = RuntimeError(f"HTTP {r.status_code} from {url}")
            except httpx.HTTPError as e:
                last = RuntimeError(f"{type(e).__name__} from {url}")
        await asyncio.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Overpass unavailable: {last}")


def _place(el: dict, lat: float, lon: float) -> dict:
    t = el.get("tags", {})
    plat = el.get("lat") or (el.get("center") or {}).get("lat")
    plon = el.get("lon") or (el.get("center") or {}).get("lon")
    # many small mosques are mapped without a name, or only with a name in the local language
    name = t.get("name:en") or t.get("name") or t.get("int_name") or t.get("official_name") or \
        next((v for k, v in sorted(t.items()) if k.startswith("name:") and v), "")
    mosque = t.get("religion") == "muslim" and t.get("amenity") == "place_of_worship"
    return {
        "osm_id": f"{el['type']}/{el['id']}",
        "name": name or ("Mosque" if mosque else ""),
        "unnamed": not name,
        "name_local": t.get("name", ""),
        "lat": plat, "lon": plon,
        "distance_m": round(distance_m(lat, lon, plat, plon)) if plat else None,
        "cuisine": t.get("cuisine", "").replace(";", ", "),
        "halal": t.get("diet:halal", ""),
        "address": " ".join(x for x in [t.get("addr:street", ""), t.get("addr:housenumber", "")] if x),
        "website": t.get("website") or t.get("contact:website", ""),
        "opening_hours": t.get("opening_hours", ""),
        "kind": t.get("amenity") or t.get("shop") or t.get("leisure") or "",
        # a bar inside, or beer/wine on the menu: shown as "Serves alcohol"
        "alcohol": t.get("bar") == "yes" or any(t.get(k) in ("yes", "served", "only")
                                                for k in ("drink:alcohol", "drink:beer", "drink:wine", "drink:spirits")),
    }


CACHE_DIR = CACHE / "osm"
_SEED_NAME = re.compile(r"^v2_(-?\d+(?:\.\d+)?)_(-?\d+(?:\.\d+)?)_(\d+)(_af)?\.json$")


def _nearest_file(folder, lat: float, lon: float, radius_m: int, all_food: bool, within_m: int = 1500):
    """A shipped area file of the same kind whose centre is within 1.5 km of this point."""
    best = None
    for f in folder.glob("v2_*.json") if folder.exists() else []:
        m = _SEED_NAME.match(f.name)
        if not m or int(m.group(3)) < radius_m or bool(m.group(4)) != all_food:
            continue
        d = distance_m(lat, lon, float(m.group(1)), float(m.group(2)))
        if d <= within_m and (best is None or d < best[0]):
            best = (d, f)
    return best[1] if best else None


EATERIES = ("restaurant", "fast_food", "cafe", "food_court")  # a halal-tagged mini-golf or butcher is not a meal


async def area(lat: float, lon: float, radius_m: int = 3000, all_food: bool = False) -> dict:
    """Halal food, mosques and sights around a point in ONE Overpass query (the public server is slow, ~20 s),
    cached on disk per ~1 km grid cell so each area is fetched only once."""
    key = f"v2_{round(lat, 2)}_{round(lon, 2)}_{radius_m}" + ("_af" if all_food else "")
    if (hit := _cached("area:" + key)) is not None:
        return hit
    f, seed = CACHE_DIR / f"{key}.json", SEED / "osm" / f"{key}.json"
    # an area fetched for a point a few hundred metres away covers this one too ("Istanbul" vs "Sultanahmet")
    seed = seed if seed.exists() else _nearest_file(SEED / "osm", lat, lon, radius_m, all_food) or seed
    if f.exists() and time.time() - f.stat().st_mtime < 14 * 86400:
        data = json.loads(f.read_text(encoding="utf-8"))
    elif seed.exists():
        data = json.loads(seed.read_text(encoding="utf-8"))
    else:
        a = f"(around:{radius_m},{lat},{lon})"
        # three separate outputs so a city full of mosques cannot crowd out the restaurants
        # in Muslim-majority countries almost nothing is tagged halal (everything is): take all eateries
        food = (f'nwr["amenity"~"^(restaurant|fast_food|cafe|food_court)$"]["name"]{a};' if all_food
                else f'nwr["diet:halal"~"^(yes|only)$"]["name"]{a};')
        q = (f'[out:json][timeout:90];'
             f'{food}out center tags 300;'
             f'nwr["amenity"="place_of_worship"]["religion"="muslim"]{a};out center tags 200;'
             f'(nwr["tourism"~"^(attraction|museum|viewpoint|gallery|zoo|aquarium|theme_park)$"]["name"]{a};'
             f'nwr["leisure"="park"]["name"]["wikidata"]{a};nwr["historic"]["name"]["wikidata"]{a};);out center tags 250;')
        data = await _overpass(q)
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass
    halal, mosq, sights, seen = [], [], [], set()
    for e in data:
        uid = f"{e['type']}/{e['id']}"
        if uid in seen:
            continue
        seen.add(uid)
        t = e.get("tags", {})
        p = _place(e, lat, lon)
        if not p["lat"] or p["kind"] in ("bar", "pub", "nightclub", "biergarten"):
            continue
        if t.get("religion") == "muslim" and t.get("amenity") == "place_of_worship":
            mosq.append(p)
        elif t.get("amenity") in EATERIES and (t.get("diet:halal") in ("yes", "only") or all_food):
            if t.get("diet:halal") == "no" or "beer" in t.get("cuisine", "") or t.get("drink:beer") == "yes" and all_food and not t.get("diet:halal"):
                continue
            halal.append(p)
        elif not (t.get("tourism") or t.get("leisure") == "park" or t.get("historic")):
            continue  # came in only through the halal query (e.g. a mini-golf): neither a meal nor a sight
        else:
            p["kind"] = t.get("tourism") or ("park" if t.get("leisure") == "park" else "historic")
            p["wikidata"] = t.get("wikidata", "")
            sights.append(p)
    out = {k: sorted(v, key=lambda p: p["distance_m"]) for k, v in (("halal", halal), ("mosques", mosq), ("sights", sights))}
    _cache["area:" + key] = (time.time(), out)
    return out


async def halal_food(lat: float, lon: float, radius_m: int = 1500, limit: int = 25) -> list[dict]:
    """Restaurants, cafes and shops tagged diet:halal=yes|only, nearest first."""
    found = (await area(lat, lon, max(radius_m, 3000)))["halal"]
    return [p for p in found if p["distance_m"] <= radius_m][:limit]


async def mosques(lat: float, lon: float, radius_m: int = 1500, limit: int = 10) -> list[dict]:
    found = (await area(lat, lon, max(radius_m, 3000)))["mosques"]
    return [p for p in found if p["distance_m"] <= radius_m][:limit]


async def sights(lat: float, lon: float, radius_m: int = 3000, limit: int = 30) -> list[dict]:
    found = (await area(lat, lon, max(radius_m, 3000)))["sights"]
    return [p for p in found if p["distance_m"] <= radius_m][:limit]


async def nearest_mosque(lat: float, lon: float) -> dict | None:
    found = (await area(lat, lon))["mosques"]
    return found[0] if found else None
