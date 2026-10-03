"""Rihla agent: an LLM with tools plans a trip that fits the traveller's taste AND faith.

The model decides which tools to call and finishes with a structured itinerary. Qloo is behind
every kind of stop: the traveller's favourite films/books/artists become Qloo taste signals, and
sights, halal restaurants and mosques near the destination are ranked by those signals plus the
"Islam" and "Parents with young children" audiences. OpenStreetMap adds halal tags and more mosques.
Code then attaches coordinates, halal confidence and the nearest mosque to every stop, so nothing in
the plan is invented.
"""
import datetime as dt
import json

import asyncio
import re
import time
from difflib import SequenceMatcher

from openai import APIError, AsyncOpenAI, BadRequestError, RateLimitError

from . import config, halal, osm, prayer, qloo

MAX_STEPS = 6
COMPOSE_THINK = False  # Nemotron's reasoning on the final plan: ~70 s instead of ~20 s, little better in tests
AREA_M = 3000  # one OpenStreetMap area around the destination (seed_cities.py uses the same)
EMPTY_AREA = {"halal": [], "mosques": [], "sights": []}
KIDS = re.compile(r"\b(kids?|child|children|sons?|daughters?|bab(y|ies)|toddlers?|boys?|girls?|famil(y|ies))\b", re.I)
RANK = {"verified": 0, "likely": 1, "unknown": 2}
# "Orhan Pamuk's novels" -> "Orhan Pamuk", "Pixar movies" -> "Pixar"
GENERIC_WORDS = re.compile(r"'s\b|\b(movies?|films?|novels?|books?|series|shows?|tv|music|songs?|albums?|video ?games?|games?|"
                           r"franchise|saga|cartoons?|anime)\b", re.I)
NOT_A_TASTE = {"place", "locality", "destination", "album"}  # untyped search hits that are rarely what people mean
STOP = set("""love loves loved like likes enjoy enjoys into also really very much with they them their our ours we us
and the are is a an of for to in on at our my his her kids kid children child family families son sons daughter
daughters wife husband aged age years old year who what all some any lot lots big fan fans favourite favorite
especially things stuff place places food spicy good great movies movie films film books book novels novel music
songs series shows show games game""".split())
# not for young kids, whatever the taste data says
GRIM = re.compile(r"tortur|ripper|dungeon|horror|serial killer|execution|gallows|crime museum|sex|erotic|strip club", re.I)

SYSTEM = """You are Rihla, a travel planner for Muslim travellers and families.
You plan real days in a real city using ONLY places returned by your tools. Rules:
- Every stop must reference a place by its exact "ref" from a tool result. Never invent places.
- Turn 1: call taste_entities ONCE with ALL the titles and names the traveller wrote (films, books, authors, artists,
  shows, games, teams), exactly as they wrote them. Never add names they did not write.
- Turn 2: call taste_places ONCE with 2-4 categories that fit the traveller (e.g. museum, landmark, park,
  family for kids, bookstore for readers) and the refs from turn 1; in the same turn call halal_food_near and
  mosques_near for the area where each day will be.
- Food: call halal_food_near around the area of each day. Prefer halal "verified", then "likely", and places
  without "alcohol". Never suggest alcohol-centred venues (bars, pubs, wineries, nightclubs).
- Prayer: the five daily prayer times are given. Call mosques_near for each day's area and add a "prayer" stop for
  Dhuhr, Asr and Maghrib on each day (Fajr and Isha are usually at the hotel); keep the schedule realistic around them.
- If Qloo returns little, use sights_near (OpenStreetMap) as a fallback.
- Mix each day: a famous highlight ("popular") and places that fit their taste ("taste_match", "matches_their_interest").
  A place that "matches_their_interest" (e.g. known for dinosaurs, for kids who love dinosaurs) is a must.
- Keep walking distances short: cluster each day around one area. Families with kids: shorter days, parks, museums.
- Never use the same sight or restaurant twice in the trip (mosques may repeat). Give each day a different area or theme.
- Be efficient: 2-3 turns in total.
- "why" must be one short sentence: what the place is or is known for (from its "about" and "known_for") and why it
  suits THIS traveller (kids' ages, interests, faith). Do not mention "fans" or taste data: the app shows Qloo's
  taste matches next to each stop. Only state facts you can see in the tool results; never invent links.
- Every day needs lunch and dinner from the food results, and prayer stops at mosques from mosques_near.
- When you have what you need, stop calling tools and say READY. You will then be asked for the final JSON.
  Write all text in {language}."""

TOOLS = [
    {"type": "function", "function": {
        "name": "taste_entities",
        "description": "Find the Qloo taste-graph entities for ALL the things the traveller likes (films, books, authors, artists, TV shows, brands, video games) in one call. Returns refs to use as taste signals.",
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "query": {"type": "string", "description": "e.g. 'Ratatouille', 'Orhan Pamuk', 'Sami Yusuf'"},
                "kind": {"type": "string", "enum": ["movie", "tv_show", "book", "author", "artist", "album", "video_game", "brand", "person", "podcast"]}},
                "required": ["query", "kind"]}}},
            "required": ["items"]}}},
    {"type": "function", "function": {
        "name": "taste_places",
        "description": "Qloo: places of several categories near a point, ranked by the traveller's taste signals and by what Muslim travellers and families enjoy. No bars. Places that fit the traveller's taste are marked taste_match; popular = a famous highlight.",
        "parameters": {"type": "object", "properties": {
            "categories": {"type": "array", "items": {"type": "string", "enum": list(qloo.CATEGORIES)}},
            "interest_refs": {"type": "array", "items": {"type": "string"}, "description": "refs from taste_entities"},
            "lat": {"type": "number"}, "lon": {"type": "number"},
            "radius_m": {"type": "integer", "default": 5000}},
            "required": ["categories", "interest_refs"]}}},
    {"type": "function", "function": {
        "name": "halal_food_near",
        "description": "Halal restaurants near a point: Qloo's halal restaurants ranked by the traveller's taste, merged with halal-tagged places from OpenStreetMap, each with an honest halal confidence level.",
        "parameters": {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"},
                                                        "radius_m": {"type": "integer", "default": 1500}},
                       "required": ["lat", "lon"]}}},
    {"type": "function", "function": {
        "name": "mosques_near",
        "description": "Mosques and prayer rooms near a point (Qloo + OpenStreetMap), nearest first.",
        "parameters": {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}},
                       "required": ["lat", "lon"]}}},
    {"type": "function", "function": {
        "name": "sights_near",
        "description": "Fallback: museums, landmarks, parks and viewpoints near a point (OpenStreetMap).",
        "parameters": {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"},
                                                        "radius_m": {"type": "integer", "default": 3000}},
                       "required": ["lat", "lon"]}}},
]
PLAN_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string"},
    "summary": {"type": "string", "description": "2-3 sentences: who this trip is for and its idea"},
    "days": {"type": "array", "items": {"type": "object", "properties": {
        "day": {"type": "integer"}, "theme": {"type": "string"},
        "stops": {"type": "array", "items": {"type": "object", "properties": {
            "time": {"type": "string", "description": "HH:MM"},
            "kind": {"type": "string", "description": "one of: sight, meal, prayer, rest"},
            "ref": {"type": "string", "description": "exact ref from a tool result"},
            "why": {"type": "string"}},
            "required": ["time", "kind", "ref", "why"]}}},
        "required": ["day", "theme", "stops"]}},
    "tips": {"type": "array", "items": {"type": "string"}}},
    "required": ["title", "summary", "days"]}
COMPOSE = ("You have gathered enough. Now write the final itinerary as ONE JSON object, no prose, with this shape: "
           + json.dumps(PLAN_SCHEMA) + " Use only refs you received from tools. Write all text in {language}.")


class LLM:
    """Primary model with a fallback provider; retries briefly on rate limits."""

    def __init__(self):
        # Nemotron: turning the visible "thinking" off makes each call ~2 s instead of 5-30 s
        nv = {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}} if "nvidia" in config.LLM_BASE_URL else {}
        self.clients = [(AsyncOpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL, timeout=150), config.LLM_MODEL, nv)]
        if config.FALLBACK_API_KEY:
            self.clients.append((AsyncOpenAI(api_key=config.FALLBACK_API_KEY, base_url=config.FALLBACK_BASE_URL, timeout=90),
                                 config.FALLBACK_MODEL, {}))
        self.used = config.LLM_MODEL
        self.log: list[dict] = []

    async def chat(self, think: bool = False, **kw):
        """think=True keeps Nemotron's reasoning on (slower, better) — used for the final itinerary."""
        last: Exception | None = None
        for client, model, extra in self.clients:
            if think:
                extra = {}
            for attempt in range(3):
                try:
                    t0 = time.time()
                    r = await client.chat.completions.create(model=model, **kw, **extra)
                    self.used = model
                    self.log.append({"model": model, "s": round(time.time() - t0, 1),
                                     "out": getattr(r.usage, "completion_tokens", None), "think": not extra})
                    return r
                except BadRequestError:
                    raise
                except RateLimitError as e:
                    last = e
                    await asyncio.sleep(4 * (attempt + 1))
                except APIError as e:  # 5xx / timeouts: try the next provider
                    last = e
                    break
        raise RuntimeError(f"All language models failed: {last}")


def _json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", text or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


class Trip:
    """State of one planning run: the places the tools returned (by ref), the traveller's Qloo taste
    signals, and a trace for the UI."""

    def __init__(self, dest: dict, travellers: str = "", tastes: str = ""):
        self.dest = dest
        # words the traveller used ("dinosaurs", "football"): matched against what reviewers say a place is known for
        self.topics = {_singular(w) for w in re.findall(r"[a-z]{4,}", (tastes or "").lower()) if w not in STOP}
        self.tastes = tastes or ""
        self.places: dict[str, dict] = {}
        self.pool: dict[str, dict[str, dict]] = {"halal": {}, "mosques": {}, "sights": {}}
        self.trace: list[dict] = []
        self.model = ""
        self.entities: dict[str, dict] = {}  # every Qloo entity seen in taste_entities, by id
        self.signals: dict[str, str] = {}    # the traveller's taste signals: Qloo id -> name
        ages = [int(a) for a in re.findall(r"\b(\d{1,2})\b", travellers or "")]
        self.kids = bool(KIDS.search(travellers or "")) and (not ages or min(ages) <= 12) or \
            bool(re.search(r"\b(bab(y|ies)|toddlers?|infants?)\b", travellers or "", re.I))
        self.audiences = [qloo.AUDIENCES["muslim"]] + ([qloo.AUDIENCES["kids"]] if self.kids else [])
        self.osm_task: asyncio.Task | None = None
        self._osm: dict | None = None

    async def osm(self, wait: float = 20.0) -> dict:
        """The OpenStreetMap area, fetched in the background; empty if it is not ready in time
        (the public Overpass server takes 20-140 s for a new city)."""
        if self._osm is not None:
            return self._osm
        if self.osm_task is None:
            return EMPTY_AREA
        try:
            self._osm = await asyncio.wait_for(asyncio.shield(self.osm_task), wait)
        except asyncio.TimeoutError:
            return EMPTY_AREA
        except Exception:
            self._osm = EMPTY_AREA
        return self._osm

    def keep(self, ref: str, p: dict, source: str, pool: str | None = None) -> dict:
        """Remember a place the model may reference; in a pool, one entry per venue (Qloo and OSM often both have it)."""
        p = dict(p, ref=ref, source=source)
        self.places[ref] = p
        if pool and p.get("lat") is not None:
            same = _same_venue(p, self.pool[pool].values(), by_distance=pool == "mosques")
            if same is None:
                self.pool[pool][ref] = p
            elif same["source"] == "osm" and source == "qloo":  # keep Qloo's richer record (photo, taste match)
                del self.pool[pool][same["ref"]]
                p.update({k: same[k] for k in ("halal", "cuisine") if same.get(k) and not p.get(k)})
                self.places[ref] = p
                self.pool[pool][ref] = p
        return p

    def near(self, pool: str, lat: float, lon: float, radius_m: int, limit: int) -> list[dict]:
        out = []
        for p in self.pool[pool].values():
            d = round(osm.distance_m(lat, lon, p["lat"], p["lon"]))
            if d <= radius_m:
                out.append(dict(p, distance_m=d))
        return sorted(out, key=lambda p: p["distance_m"])[:limit]

    def absorb_osm(self, area: dict) -> None:
        """Add OpenStreetMap mosques and halal food to the pools, skipping venues Qloo already gave us."""
        cc = self.dest.get("country_code", "")
        for kind in ("mosques", "halal"):
            for p in area.get(kind, []):
                ref = "osm:" + p["osm_id"]
                if ref in self.places or any(x.get("osm_id") == p["osm_id"] for x in self.pool[kind].values()):
                    continue
                if kind == "halal":
                    lvl, why = halal.level(p, cc)
                    p = dict(p, halal_level=lvl, halal_reason=why)
                self.keep(ref, dict(p, kind="mosque") if kind == "mosques" else p, "osm", kind)


_GENERIC = re.compile(r"\b(the|restaurant|mosque|masjid|cami|camii|mescidi|islamic|centre|center)\b")


def _same_venue(p: dict, others, by_distance: bool = False) -> dict | None:
    """The entry in others that is the same venue as p: a similar name within 200 m
    (for mosques also anything within 80 m: two mosques are never that close)."""
    a = " ".join(_GENERIC.sub(" ", halal._norm(p.get("name", ""))).split())
    for o in others:
        d = osm.distance_m(p["lat"], p["lon"], o["lat"], o["lon"])
        if by_distance and d < 80:
            return o
        if d < 200:
            b = " ".join(_GENERIC.sub(" ", halal._norm(o.get("name", ""))).split())
            if a and b and (a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a)) or SequenceMatcher(None, a, b).ratio() >= 0.82):
                return o
    return None


def _qplace(it: dict, kind: str, trip: Trip, lat: float, lon: float, because: list[str] | None = None) -> dict:
    """A Qloo place as Rihla's place dict; 'because' = the traveller's tastes it matches."""
    because = because or []
    hits = [_as_written(trip.tastes, k) for k in it.get("known_for") or []
            if trip.topics & {_singular(w) for w in re.findall(r"[a-z]{4,}", k.lower())}][:2]
    p = {k: it.get(k) for k in ("name", "lat", "lon", "address", "image", "website", "rating", "categories", "affinity",
                                "description", "known_for", "hours")}
    p["kids_ok"] = (it.get("kids") or 0) >= 0.3
    p["topic"] = hits
    p.update(kind=kind, because=because, qloo_halal=qloo.halal_signal(it), alcohol=qloo.serves_alcohol(it), qloo_id=it["id"],
             distance_m=round(osm.distance_m(lat, lon, it["lat"], it["lon"])))
    return p


def _compact(p: dict) -> dict:
    """What the model sees of a place (short: tool results go back into the prompt)."""
    keys = ("ref", "name", "kind", "categories", "cuisine", "halal_level", "alcohol", "distance_m", "lat", "lon", "affinity")
    out = {k: p[k] for k in keys if p.get(k) not in (None, "", [])}
    if p.get("description"):
        out["about"] = p["description"][:130]
    if p.get("known_for"):
        out["known_for"] = p["known_for"][:4]
    if out.get("categories"):
        out["categories"] = out["categories"][:2]
    if p.get("kids_ok"):
        out["good_for_kids"] = True
    if p.get("popular"):
        out["popular"] = True
    if p.get("because"):
        out["taste_match"] = True  # who the fans are is shown in the app; the model would retell it as a fact
    if p.get("topic"):
        out["matches_their_interest"] = p["topic"]
    for k in ("lat", "lon"):
        if k in out:
            out[k] = round(out[k], 4)
    if "affinity" in out:
        out["affinity"] = round(out["affinity"], 2)
    return out


def _point(trip: Trip, args: dict) -> tuple[float, float]:
    lat, lon = args.get("lat"), args.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or \
            osm.distance_m(lat, lon, trip.dest["lat"], trip.dest["lon"]) > 30000:
        return trip.dest["lat"], trip.dest["lon"]  # missing or far away (a hallucinated point): use the destination
    return float(lat), float(lon)


async def _run_tool(trip: Trip, name: str, args: dict) -> tuple[list | dict, str]:
    """Run one tool; returns (result for the model, a short note for the 'How Rihla planned this' trace)."""
    if name == "taste_entities":
        items = [x for x in args.get("items") or [args] if isinstance(x, dict) and str(x.get("query", "")).strip()][:8]
        skipped = [x for x in items if not _named(trip.tastes, str(x["query"]))]
        items = [x for x in items if x not in skipped]
        done = await asyncio.gather(*[_lookup(trip, str(x["query"]).strip(), x.get("kind")) for x in items])
        out = {str(x["query"]).strip(): res for x, (res, _) in zip(items, done)}
        notes = [f"{x['query']} → {note}" for x, (_, note) in zip(items, done)]
        for x in skipped:  # a topic ("history") or a name the model made up: matched against what places are known for
            out[str(x["query"]).strip()] = []
            notes.append(f"{x['query']} → used as a topic")
        return out, "; ".join(notes)
    if name == "taste_places":
        cats = list(dict.fromkeys(c for c in (args.get("categories") or [args.get("category")]) if c in qloo.CATEGORIES))[:4]
        out = await _taste_places(trip, cats or ["landmark", "museum"], args)
        names = [trip.signals[r] for r in list(trip.signals)[:5]]
        return out, ", ".join(out) + (" · taste: " + ", ".join(names) if names else "")
    return await _run_place_tool(trip, name, args)


PEOPLE = {"urn:entity:artist", "urn:entity:person", "urn:entity:actor", "urn:entity:director"}
WORKS = {"urn:entity:movie", "urn:entity:tv_show", "urn:entity:book", "urn:entity:brand", "urn:entity:videogame",
         "urn:entity:podcast"}


async def _lookup(trip: Trip, query: str, kind: str | None) -> tuple[list, str]:
    """One favourite -> Qloo entities; the best match becomes one of the traveller's taste signals.
    Score = how well the name matches + the requested type + Qloo popularity, so "Harry Potter" (a book)
    beats the unknown TV show of the same name, and "Marvel movies" never becomes a singer called Märvel."""
    kind = kind if kind in qloo.TYPES else None
    q = " ".join(GENERIC_WORDS.sub(" ", query).split()) or query
    key, want = _key(q), qloo.TYPES.get(kind or "", "")
    res = await asyncio.gather(qloo.search(q, None, take=8), qloo.search(q, kind, take=5) if kind else asyncio.sleep(0, []),
                               return_exceptions=True)
    cands, places = {}, []
    for e in [x for r in res if isinstance(r, list) for x in r]:
        if str(e["type"]).rsplit(":", 1)[-1] not in NOT_A_TASTE:
            cands.setdefault(e["id"], e)
        elif e["type"] == "urn:entity:place" and e.get("lat") is not None and \
                osm.distance_m(e["lat"], e["lon"], trip.dest["lat"], trip.dest["lon"]) < 40000 and \
                (_key(e["name"]) == key or re.search(r"\b" + re.escape(key) + r"\b", _key(e["name"]))):
            places.append(e)
    if places:  # they want to go there: a must-visit stop
        it = max(places, key=lambda e: (_key(e["name"]) == key, e.get("popularity") or 0))
        p = _qplace(it, "landmark", trip, trip.dest["lat"], trip.dest["lon"])
        p["requested"] = True
        # "Burj Khalifa" is one place: its words must not match other places' keywords ("Burj al Arab")
        trip.topics -= {_singular(w) for w in re.findall(r"[a-z]{4,}", key)}
        ref = "qloo:" + str(it["id"])
        trip.keep(ref, p, "qloo", "sights")
        return [{"ref": ref, "name": it["name"], "type": "place",
                 "note": "a place the traveller wants to visit: include it in the plan"}], f"{it['name']} (a place to visit)"

    def score(e: dict) -> float:
        name = _key(e["name"])
        whole = bool(key) and re.search(r"\b" + re.escape(key) + r"\b", name)  # "marvel" is not in "marvelous"
        match = 1.0 if name == key else 0.7 if whole else 0.3 if SequenceMatcher(None, key, name).ratio() >= 0.8 else 0
        if not match:
            return 0
        person = e["type"] in PEOPLE or (e["type"] == "urn:entity:author" and want != "urn:entity:book")
        fit = 0.3 if e["type"] == want else -0.3 if want in WORKS and person else 0
        return match + fit + 0.5 * float(e.get("popularity") or 0)
    ranked = sorted((e for e in cands.values() if score(e) > 0), key=score, reverse=True)
    for e in ranked:
        trip.entities[str(e["id"])] = e
    if not ranked:  # e.g. a topic like "Ottoman history": no signal; its words still match what places are known for
        return [], "no match"
    top = ranked[0]
    # show the traveller's own words ("Percy Jackson"), not "The Lightning Thief (Percy Jackson and the Olympians, #1)"
    shown = q if re.search(r"\b" + re.escape(key) + r"\b", _key(top["name"])) and len(top["name"]) > len(q) else top["name"]
    trip.signals.setdefault(str(top["id"]), shown)
    note = f"{top['name']} ({str(top['type']).rsplit(':', 1)[-1].replace('_', ' ')})"
    return [{"ref": "qloo:" + str(e["id"]), "name": e["name"], "type": str(e["type"]).rsplit(":", 1)[-1]} for e in ranked[:3]], note


def _named(text: str, query: str) -> bool:
    """Is this a title or name the traveller wrote? Its words must be in their text, and one of them capitalised
    there mid-sentence ("Orhan Pamuk", "Minecraft"): "history" or "sea views" are topics, not Qloo entities.
    Text written all in lower case gives no such hint: then any words from it count."""
    words = [w for w in re.findall(r"[^\W_]+", query) if len(w) >= 3 and w.lower() not in STOP]
    if not words:
        return False
    found = [m for w in words for m in re.finditer(r"\b" + re.escape(w) + r"\b", text, re.I)]
    if len({m.group(0).lower() for m in found}) < (len(words) + 1) // 2:
        return False
    mid = lambda i: not re.search(r"(^|[.!?]\s*)$", text[:i])  # not the first word of a sentence
    if not any(text[m.start()].isupper() and mid(m.start()) for m in re.finditer(r"\b\w", text)):
        return True  # no capitals at all: cannot tell names from topics
    return any(m.group(0)[0].isupper() and mid(m.start()) for m in found)


def _as_written(text: str, phrase: str) -> str:
    """'harry potter' (as reviewers tag it) -> 'Harry Potter' when the traveller wrote it with capitals."""
    out = []
    for w in phrase.split():
        m = re.search(r"\b" + re.escape(w) + r"\b", text, re.I)
        out.append(m.group(0) if m and m.group(0)[0].isupper() else w)
    return " ".join(out)


def _singular(w: str) -> str:
    return w[:-3] + "y" if w.endswith("ies") else w[:-1] if w.endswith("s") and not w.endswith("ss") else w


def _key(name: str) -> str:
    return " ".join(re.sub(r"^the\s+", "", halal._norm(name)).split())


async def _taste_places(trip: Trip, cats: list[str], args: dict) -> dict:
    """Qloo places per category, ranked by all the traveller's taste signals together. Each signal is also
    asked on its own: a place is "because" of a taste when it is in that taste's own top 10, and every taste
    gets its best place of each category into the list (so each family member finds something they love)."""
    cc = trip.dest.get("country_code", "")
    refs = [str(r).split(":", 1)[-1] for r in args.get("interest_refs") or []]
    refs = [r for r in dict.fromkeys(refs) if r in trip.signals] or list(trip.signals)  # ignore refs the model made up
    lat, lon = _point(trip, args)
    radius = min(max(int(args.get("radius_m") or 5000), 4500), 8000)  # a city's highlights are often 3-4 km apart
    union = tuple(dict.fromkeys(t for c in cats for t in qloo.CATEGORIES[c]))
    # "tourist attraction" also tags restaurants and shops: keep them out unless the model asked for cafes or shopping
    avoid = (() if "cafe" in cats else qloo._cat("restaurant", "fast_food_restaurant", "bar_and_grill")) + \
            (() if "shopping" in cats else qloo._cat("clothing_store", "department_store", "shopping_mall", "gift_shop"))
    ask = dict(audiences=trip.audiences, lat=lat, lon=lon, radius_m=radius, exclude_tags=avoid)
    solo = refs[:5] if len(refs) > 1 else []
    res = await asyncio.gather(qloo.insights("place", interests=refs, tags=union, take=50, **ask),
                               qloo.insights("place", tags=union, take=30, lat=lat, lon=lon, radius_m=radius,
                                             exclude_tags=avoid),  # the city's best-loved places: no taste, no audience
                               *[qloo.insights("place", interests=[r], tags=union, take=20, **ask) for r in solo],
                               return_exceptions=True)
    ok = lambda it: it.get("lat") is not None and not (trip.kids and GRIM.search(it["name"] + " " + it.get("description", "")))
    together, famous = [[it for it in x if ok(it)] if isinstance(x, list) else [] for x in res[:2]]
    combined = [[it for it in together if set(it["tag_ids"]) & set(qloo.CATEGORIES[c])] for c in cats]
    own = {r: [it for it in x if ok(it)] if isinstance(x, list) else [] for r, x in zip(solo, res[2:])}
    popular = {it["id"] for it in famous[:12]}
    everything = {it["id"]: it for lst in [together, famous, *own.values()] for it in lst}

    def topic(it: dict) -> bool:
        return any(trip.topics & {_singular(w) for w in re.findall(r"[a-z]{4,}", k.lower())} for k in it.get("known_for") or [])
    top10 = {r: [it["id"] for it in items[:10]] for r, items in own.items()}

    def because(pid: str) -> list[str]:
        if len(refs) == 1:
            return [trip.signals[refs[0]]]
        return [trip.signals[r] for _, r in sorted((ids.index(pid), r) for r, ids in top10.items() if pid in ids)][:2]

    out, seen = {}, set()
    for c, items in zip(cats, combined):
        tags = set(qloo.CATEGORIES[c])
        mine = lambda lst: [it for it in lst if set(it["tag_ids"]) & tags]
        for r, theirs in own.items():  # each taste's best place of this category
            pick = next(iter(mine(theirs[:8])), None)
            if pick and pick["id"] not in {it["id"] for it in items[:8]}:
                items.insert(min(3, len(items)), pick)
        for it in mine(famous)[:2][::-1]:  # two famous highlights
            if it["id"] not in {x["id"] for x in items[:8]}:
                items.insert(min(4, len(items)), it)
        items = [it for it in mine(everything.values()) if topic(it)] + items  # what they said they love, first
        lst = []
        for it in items:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            p = _qplace(it, c, trip, lat, lon, because(it["id"]))
            p["popular"] = it["id"] in popular
            if c == "cafe":
                p["halal_level"], p["halal_reason"] = halal.level(p, cc)
            lst.append(_compact(trip.keep("qloo:" + str(it["id"]), p, "qloo", "sights")))
            if len(lst) == 8:
                break
        out[c] = lst
    return out


async def _run_place_tool(trip: Trip, name: str, args: dict) -> tuple[list | dict, str]:
    cc = trip.dest.get("country_code", "")
    if name == "halal_food_near":
        lat, lon = _point(trip, args)
        radius = min(max(int(args.get("radius_m") or 1500), 800), 3000)
        # in Muslim-majority countries almost no restaurant is tagged halal (nearly all food is): ask for restaurants
        tags = qloo.RESTAURANT if cc in halal.MUSLIM_MAJORITY else (qloo.HALAL_CUISINE,)
        radius = max(radius, 2000)
        items, area = await asyncio.gather(
            qloo.insights("place", interests=list(trip.signals), audiences=trip.audiences, lat=lat, lon=lon,
                          radius_m=3000, tags=tags, exclude_tags=qloo._cat("hotel", "lodging"), take=25),
            trip.osm(15), return_exceptions=True)
        items = items if isinstance(items, list) else []
        area = area if isinstance(area, dict) else EMPTY_AREA
        found = []
        for it in items:
            if it.get("lat") is None:
                continue
            p = _qplace(it, "restaurant", trip, lat, lon)
            match = halal.match_verified(p, area["halal"])
            if match:  # the same venue is tagged halal on OpenStreetMap
                p.update(halal=match.get("halal"), cuisine=match.get("cuisine", ""), osm_id=match["osm_id"])
            p["halal_level"], p["halal_reason"] = halal.level(p, cc)
            found.append(trip.keep("qloo:" + str(it["id"]), p, "qloo", "halal"))
        trip.absorb_osm(area)
        pool = trip.near("halal", lat, lon, radius, 60)
        pool.sort(key=lambda p: (RANK.get(p.get("halal_level"), 3), bool(p.get("alcohol")), -(p.get("affinity") or 0), p["distance_m"]))
        # the most certain options first, plus the best taste matches from Qloo so the model can pick for taste too
        best = pool[:10] + [p for p in pool[10:] if p.get("source") == "qloo" and p.get("because")][:5]
        listed = sum(1 for p in best if p.get("halal_level") == "verified")
        return [_compact(p) for p in best], f"{len(found)} from Qloo, {listed} listed halal"
    if name == "mosques_near":
        lat, lon = _point(trip, args)
        items, area = await asyncio.gather(
            qloo.insights("place", lat=lat, lon=lon, radius_m=4000, tags=qloo.MOSQUE, take=15),
            trip.osm(15), return_exceptions=True)
        n_q = 0
        for it in items if isinstance(items, list) else []:
            if it.get("lat") is not None:
                trip.keep("qloo:" + str(it["id"]), _qplace(it, "mosque", trip, lat, lon), "qloo", "mosques")
                n_q += 1
        trip.absorb_osm(area if isinstance(area, dict) else EMPTY_AREA)
        near = trip.near("mosques", lat, lon, 5000, 8)
        return [_compact(p) for p in near], f"{n_q} from Qloo, {len(near)} nearby"
    if name == "sights_near":
        lat, lon = _point(trip, args)
        radius = min(max(int(args.get("radius_m") or 3000), 2000), 5000)
        area = await trip.osm(25)
        out = []
        for p in area["sights"]:
            d = round(osm.distance_m(lat, lon, p["lat"], p["lon"]))
            if d <= radius:
                out.append(_compact(trip.keep("osm:" + p["osm_id"], dict(p, distance_m=d), "osm", "sights")))
        if not out:  # OpenStreetMap is slow or empty here: Qloo's best-loved sights instead
            items = await qloo.insights("place", audiences=trip.audiences, lat=lat, lon=lon, radius_m=radius,
                                        tags=qloo.CATEGORIES["landmark"] + qloo.CATEGORIES["museum"] + qloo.CATEGORIES["park"], take=20)
            out = [_compact(trip.keep("qloo:" + str(it["id"]), _qplace(it, "landmark", trip, lat, lon), "qloo", "sights"))
                   for it in items if it.get("lat") is not None]
        return sorted(out, key=lambda p: p.get("distance_m", 0))[:20], f"{len(out)} sights"
    return {"error": f"unknown tool {name}"}, ""


def _fit(result, limit: int) -> str:
    """JSON for the model, never cut in the middle: drop the last items of the longest lists until it fits."""
    text = json.dumps(result, ensure_ascii=False)
    while len(text) > limit:
        lists = [v for v in (result.values() if isinstance(result, dict) else [result]) if isinstance(v, list) and v]
        if not lists:
            return text[:limit]
        max(lists, key=len).pop()
        text = json.dumps(result, ensure_ascii=False)
    return text


def _count(result) -> int:
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict) and "error" not in result:
        return sum(len(v) for v in result.values() if isinstance(v, list))
    return 0


def _centres(points: list[tuple[float, float]], k: int) -> list[tuple[float, float]]:
    """k-means on the taste places: one centre per day (each day is clustered around one area)."""
    if len(points) <= k:
        return points
    cs = [points[int(i * len(points) / k)] for i in range(k)]
    for _ in range(12):
        groups: list[list] = [[] for _ in cs]
        for p in points:
            groups[min(range(len(cs)), key=lambda i: osm.distance_m(p[0], p[1], cs[i][0], cs[i][1]))].append(p)
        cs = [(sum(x[0] for x in g) / len(g), sum(x[1] for x in g) / len(g)) if g else cs[i] for i, g in enumerate(groups)]
    return cs


async def _fill_gaps(trip: Trip, days: int, emit) -> dict:
    """Halal food and mosques near each day's likely area that the model has not looked up yet."""
    pts = [(p["lat"], p["lon"]) for p in trip.pool["sights"].values() if p.get("source") == "qloo"][:30]
    centres = _centres(pts, days) or [(trip.dest["lat"], trip.dest["lon"])]
    asked = {t: [(a["lat"], a["lon"]) for a in (x["args"] for x in trip.trace if x["tool"] == t)
                 if isinstance(a.get("lat"), (int, float)) and isinstance(a.get("lon"), (int, float))]
             for t in ("halal_food_near", "mosques_near")}
    jobs = []
    for i, (lat, lon) in enumerate(centres):
        for tool in ("halal_food_near", "mosques_near"):
            if not any(osm.distance_m(lat, lon, a, b) < 1200 for a, b in asked[tool]) and \
                    (tool != "mosques_near" or not trip.near("mosques", lat, lon, 1500, 1)):
                jobs.append((f"day {i + 1} area", tool, {"lat": round(lat, 5), "lon": round(lon, 5)}))
    if not jobs:
        return {}
    for j, (area, tool, args) in enumerate(jobs):
        await emit({"type": "step", "id": f"fill{j}", "text": _step_text(tool, args) + f" ({area})"})
    done = await asyncio.gather(*[_safe_tool(trip, tool, args) for _, tool, args in jobs])
    extra = {}
    for j, ((area, tool, args), (result, note)) in enumerate(zip(jobs, done)):
        trip.trace.append({"tool": tool, "args": args, "found": _count(result), "note": note + " (added by Rihla)"})
        await emit({"type": "found", "id": f"fill{j}", "n": _count(result)})
        extra[f"{tool} near {area} ({args['lat']}, {args['lon']})"] = result
    return extra


async def _safe_tool(trip: Trip, name: str, args: dict) -> tuple[list | dict, str]:
    try:
        return await _run_tool(trip, name, args)
    except Exception as e:  # a slow map server or a bad argument must not kill the whole plan
        return {"error": str(e)[:200]}, "error"


def _step_text(name: str, args: dict) -> str:
    """What the agent is doing, in words for the live progress list."""
    if name == "taste_entities":
        names = [str(x.get("query", "")) for x in args.get("items") or [args] if isinstance(x, dict)]
        return "Looking up " + ", ".join(f"“{n}”" for n in names if n) + " in the Qloo taste graph"
    if name == "taste_places":
        cats = args.get("categories") or [args.get("category", "places")]
        return "Asking Qloo for " + ", ".join(str(c) for c in cats) + " that match your taste"
    return {"halal_food_near": "Finding halal restaurants that fit your taste", "sights_near": "Finding museums, landmarks and parks nearby",
            "mosques_near": "Finding mosques nearby"}.get(name, name)


async def _noop(event: dict) -> None:
    return None


async def plan(destination: str, days: int, start: dt.date, travellers: str, tastes: str, language: str = "English",
               emit=_noop) -> dict:
    """emit(event) is awaited for every step so the UI can show live progress."""
    t0, q0 = time.time(), dict(qloo.stats)
    await emit({"type": "step", "text": f"Locating {destination}"})
    dest = await osm.geocode(destination)
    if not dest:
        raise ValueError(f"Could not find '{destination}' on the map")
    trip = Trip(dest, travellers, tastes)
    # OpenStreetMap can be slow for a new city: fetch it in the background while the agent works with Qloo
    trip.osm_task = asyncio.create_task(osm.area(dest["lat"], dest["lon"], AREA_M,
                                                 all_food=dest["country_code"] in halal.MUSLIM_MAJORITY))
    days = max(1, min(days, 4))
    await emit({"type": "step", "text": f"Getting prayer times for {days} day{'s' if days > 1 else ''}"})
    prayers = await asyncio.gather(*[prayer.times(dest["lat"], dest["lon"], start + dt.timedelta(days=i), dest["country_code"])
                                     for i in range(days)])
    user = {
        "destination": {"name": dest["name"], "lat": dest["lat"], "lon": dest["lon"], "country_code": dest["country_code"]},
        "days": days, "start_date": start.isoformat(), "travellers": travellers, "tastes": tastes,
        "prayer_times": [{"day": i + 1, "date": p["date"], **p["timings"]} for i, p in enumerate(prayers)],
    }
    llm = LLM()
    msgs = [{"role": "system", "content": SYSTEM.replace("{language}", language)},
            {"role": "user", "content": "Plan this trip:\n" + json.dumps(user, ensure_ascii=False)}]
    # 1) research: the model calls the tools it needs until it says it is ready
    for step in range(MAX_STEPS):
        try:
            r = await llm.chat(messages=msgs, tools=TOOLS, temperature=0.4, max_tokens=4000, tool_choice="auto")
        except BadRequestError as e:
            # Groq rejects a malformed tool call outright; tell the model and let it try again
            if "tool_use_failed" not in str(e):
                raise
            msgs.append({"role": "user", "content": "Your last tool call had invalid arguments: " + str(e)[:300] + " Try again."})
            continue
        m = r.choices[0].message
        if not m.tool_calls:
            break
        msgs.append({"role": "assistant", "content": m.content or "", "tool_calls": [tc.model_dump() for tc in m.tool_calls]})
        calls = []
        for tc in m.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append((tc, args if isinstance(args, dict) else {}))
            await emit({"type": "step", "id": tc.id, "text": _step_text(tc.function.name, args)})
        # tools of one turn run in parallel; taste lookups first, because the other tools use their signals
        results: dict[str, tuple] = {}
        for group in ([c for c in calls if c[0].function.name == "taste_entities"],
                      [c for c in calls if c[0].function.name != "taste_entities"]):
            done = await asyncio.gather(*[_safe_tool(trip, tc.function.name, a) for tc, a in group])
            results.update({tc.id: res for (tc, _), res in zip(group, done)})
        for tc, args in calls:
            result, note = results[tc.id]
            found = _count(result)
            trip.trace.append({"tool": tc.function.name, "args": {k: v for k, v in args.items() if k != "interest_refs"},
                               "found": found, "note": note})
            await emit({"type": "found", "id": tc.id, "n": found})
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": _fit(result, 16000)})
    # 2) make sure every day's area has halal food and a mosque, even if the model skipped those tools
    extra = await _fill_gaps(trip, days, emit)
    if not trip.places:
        raise RuntimeError("No places were found for this destination")
    # 3) compose: one JSON answer (more robust than a huge tool call)
    await emit({"type": "step", "text": "Building your day-by-day plan around the prayer times"})
    msgs.append({"role": "user", "content": (("More places I looked up for you: " + _fit(extra, 12000) + "\n\n")
                                             if extra else "") + COMPOSE.replace("{language}", language)})
    final, raw = None, ""
    for _ in range(2):
        r = await llm.chat(think=COMPOSE_THINK, messages=msgs, temperature=0.3, max_tokens=16000, response_format={"type": "json_object"})
        raw = r.choices[0].message.content or ""
        final = _json(raw)
        if final and final.get("days"):
            break
        msgs.append({"role": "user", "content": "That was not valid JSON with a non-empty days array. Output only the JSON."})
    if not final or not final.get("days"):
        raise RuntimeError(f"The planner did not finish a plan (finish_reason={r.choices[0].finish_reason}, {len(raw)} chars)")
    trip.trace.append({"tool": "compose_plan", "args": {}, "found": sum(len(d.get("stops", [])) for d in final["days"]),
                       "note": "itinerary written, every stop checked against the tool results"})
    trip.model = llm.used
    trip.llm_log = llm.log
    trip.absorb_osm(await trip.osm(0.05))  # more mosques and halal food for the checks below, if the map is ready
    result = await _enrich(final, trip, prayers)
    result["seconds"] = round(time.time() - t0)
    result["qloo_calls"] = {k: qloo.stats[k] - q0[k] for k in q0}
    return result


def _mins(hhmm: str) -> int | None:
    try:
        return int(hhmm[:2]) * 60 + int(hhmm[3:5])
    except (ValueError, IndexError, TypeError):
        return None


def _prayer_name(hhmm: str, timings: dict) -> str:
    """The daytime prayer (Dhuhr, Asr, Maghrib) closest to hh:mm."""
    t = _mins(hhmm)
    if t is None:
        return "Prayer"
    return min(("Dhuhr", "Asr", "Maghrib"), key=lambda p: abs((_mins(timings.get(p, "")) or 9999) - t))


STOP_FIELDS = ("name", "lat", "lon", "address", "cuisine", "halal_level", "halal_reason", "website", "image", "source",
               "affinity", "because", "categories", "rating", "alcohol", "description", "kids_ok", "topic", "popular",
               "open_today", "requested")


def _fill_stop(s: dict, p: dict) -> dict:
    s.update({k: p.get(k) for k in STOP_FIELDS if p.get(k) not in (None, "", [])})
    s["place_kind"] = p.get("kind", "")
    return s


def _short(text: str, n: int = 120) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def _why(p: dict) -> str:
    """A factual 'why' for a stop the code added (no model text to reuse)."""
    about = _short(p.get("description", ""))
    if p.get("requested"):
        return f"You asked for {p.get('name', 'this place')}." + (f" {about}" if about else "")
    if p.get("topic"):
        return f"Known for {p['topic'][0]}, which you said you love." + (f" {about}" if about else "")
    kind = (p.get("categories") or [p.get("kind") or "place"])[0]
    return about or f"{kind[:1].upper() + kind[1:]} close to your other stops."


def _centre(day: dict, fallback: tuple[float, float]) -> tuple[float, float]:
    pts = [(s["lat"], s["lon"]) for s in day["stops"] if s.get("lat") is not None and s.get("kind") != "prayer"]
    return (sum(a for a, _ in pts) / len(pts), sum(b for _, b in pts) / len(pts)) if pts else fallback


DURATION = {"sight": 75, "meal": 60, "prayer": 20, "rest": 45}  # minutes a stop usually takes
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _opening(p: dict, date: str | None) -> tuple[int, int] | None | bool:
    """Qloo opening hours of a place on that date: (opens, closes) in minutes, False if closed, None if unknown."""
    try:
        slots = (p.get("hours") or {}).get(WEEKDAYS[dt.date.fromisoformat(date).weekday()])
    except (TypeError, ValueError):
        return None
    if not slots:
        return None
    if all(x.get("closed") for x in slots):
        return False
    opens = [_mins(str(x.get("opens") or "")[1:6]) for x in slots if not x.get("closed")]
    closes = [_mins(str(x.get("closes") or "")[1:6]) for x in slots if not x.get("closed")]
    opens, closes = [o for o in opens if o is not None], [c for c in closes if c is not None]
    if not opens or not closes:
        return None
    o, c = min(opens), max(closes)
    return (o, c if c > o else 24 * 60)  # closes after midnight


def _friday(date: str | None) -> bool:
    try:
        return dt.date.fromisoformat(date).weekday() == 4
    except (TypeError, ValueError):
        return False


def _jumuah(final: dict, trip: Trip) -> None:
    """On Friday the noon prayer is Jumu'ah: a congregational prayer with a sermon, held in a larger mosque.
    Rihla names it, prefers a well-known mosque near the morning's stops and keeps 45 minutes for it."""
    for day in final.get("days", []):
        if not _friday(day.get("date")):
            continue
        for st in day["stops"]:
            if st.get("kind") == "prayer" and st.get("prayer") == "Dhuhr":
                t = _mins(st.get("time", "")) or 13 * 60
                here = _near_stop(day, t, (trip.dest["lat"], trip.dest["lon"]))
                big = min((m for m in trip.near("mosques", *here, 3000, 20) if m.get("source") == "qloo"),
                          key=lambda m: m["distance_m"], default=None)
                if big and big["ref"] != st.get("ref"):
                    keep = {k: st[k] for k in ("time", "kind", "prayer")}
                    st.clear()
                    st.update(_fill_stop(dict(keep, ref=big["ref"]), big))
                st["jumuah"] = True
                st["why"] = "Jumu'ah, the Friday congregational prayer with a sermon — arrive 15 minutes early; allow about 45 minutes."
                # nothing else may start during the Friday prayer
                for other in day["stops"]:
                    o = _mins(other.get("time", "")) or 0
                    ends = o + DURATION.get(other.get("kind"), 45)
                    if other is not st and other.get("kind") != "prayer" and o < t + 45 and ends > t - 30:
                        other["time"] = _hhmm((t + 45 + 14) // 15 * 15)
                day["stops"].sort(key=lambda x: _mins(x.get("time", "")) or 0)


def _open_at(p: dict, date: str | None, t: int) -> bool:
    hours = _opening(p, date)
    return hours is None or (hours is not False and hours[0] <= t <= hours[1] - 60)


def _check_hours(final: dict, trip: Trip, drop: bool = True) -> None:
    """Visit places while they are open: move a visit to opening time, drop it if the place is closed that day."""
    for day in final.get("days", []):
        for st in list(day["stops"]):
            if st.get("kind") != "sight":
                continue
            hours = _opening(trip.places.get(st.get("ref"), {}), day.get("date"))
            t = _mins(st.get("time", "")) or 0
            if hours is False or (hours and t >= hours[1] - 45):
                if drop:
                    day["stops"].remove(st)
                continue
            if hours:
                st["open_today"] = f"{_hhmm(hours[0])}–{_hhmm(hours[1] % (24 * 60))}"
                if t < hours[0]:
                    st["time"] = _hhmm((hours[0] + 14) // 15 * 15)
        day["stops"].sort(key=lambda st: _mins(st.get("time", "")) or 0)


def _hhmm(m: int) -> str:
    m = max(0, min(m, 23 * 60 + 45))
    return f"{m // 60:02d}:{m % 60:02d}"


def _near_stop(day: dict, t: int, fallback: tuple[float, float]) -> tuple[float, float]:
    """Where the traveller is around minute t: the closest non-prayer stop in time."""
    pts = [(abs((_mins(st.get("time", "")) or 0) - t), st) for st in day["stops"] if st.get("kind") != "prayer" and st.get("lat") is not None]
    if not pts:
        return fallback
    st = min(pts, key=lambda x: x[0])[1]
    return st["lat"], st["lon"]


def _fill_days(final: dict, trip: Trip) -> None:
    """A day should not have long empty stretches: fill gaps with the best nearby place the tools found
    (famous highlights and taste matches first), and make sure there is a lunch and a dinner."""
    days = final.get("days", [])
    used = {_key(st.get("name", "")) for d in days for st in d["stops"] if st.get("kind") != "prayer"}
    target = 3 if trip.kids else 4
    home = (trip.dest["lat"], trip.dest["lon"])
    for day in days:
        # Dhuhr, Asr and Maghrib every day, at the mosque nearest to where the traveller is then
        have = {st.get("prayer") for st in day["stops"] if st.get("kind") == "prayer"}
        for name in ("Dhuhr", "Asr", "Maghrib"):
            t = _mins((day.get("prayer_times") or {}).get(name, ""))
            if name in have or t is None:
                continue
            mosque = next(iter(trip.near("mosques", *_near_stop(day, t, home), 6000, 1)), None)
            if mosque:
                day["stops"].append(_fill_stop({"time": _hhmm(t), "kind": "prayer", "prayer": name, "ref": mosque["ref"],
                                                "added_by": "rihla", "why": f"{name} at the mosque nearest to your other stops."}, mosque))
        # lunch and dinner
        for lo, hi, at, label in ((11 * 60 + 30, 14 * 60 + 30, 12 * 60 + 30, "lunch"), (18 * 60, 21 * 60, None, "dinner")):
            if any(st.get("kind") == "meal" and lo <= (_mins(st.get("time", "")) or 0) <= hi for st in day["stops"]):
                continue
            if at is None:  # dinner after Maghrib
                at = min(((_mins((day.get("prayer_times") or {}).get("Maghrib", "")) or 18 * 60) + 25), 20 * 60 + 30)
            here = _near_stop(day, at, home)
            options = [x for x in trip.near("halal", *here, 2000, 40) if _key(x["name"]) not in used]
            food = min(options, key=lambda x: (RANK.get(x.get("halal_level"), 3), bool(x.get("alcohol")), x["distance_m"]), default=None)
            if food:
                day["stops"].append(_fill_stop({"time": _hhmm(at), "kind": "meal", "ref": food["ref"], "added_by": "rihla",
                                                "why": f"A halal {label} close to your other stops."}, food))
                used.add(_key(food["name"]))
        # long empty stretches between 09:00 and 20:00
        for _ in range(4):
            if sum(1 for st in day["stops"] if st.get("kind") == "sight") >= target:
                break
            busy = sorted(((_mins(st.get("time", "")) or 0), (_mins(st.get("time", "")) or 0) + DURATION.get(st.get("kind"), 45))
                          for st in day["stops"])
            cursor, gaps = 9 * 60, []
            for a, b in busy + [(20 * 60, 20 * 60)]:
                if a - cursor >= 100:
                    gaps.append((a - cursor, cursor))
                cursor = max(cursor, b)
            if not gaps:
                break
            start = (max(gaps)[1] + 10 + 14) // 15 * 15
            here = _near_stop(day, start, home)
            seen = [(st["lat"], st["lon"]) for d in days for st in d["stops"] if st.get("kind") == "sight" and st.get("lat") is not None]
            pool = [x for x in trip.near("sights", *here, 3000, 60) if _key(x["name"]) not in used and not x.get("halal_level")
                    and _open_at(x, day.get("date"), start)
                    # not part of a place already in the plan (the Imperial Council Hall is inside Topkapi Palace)
                    and all(osm.distance_m(x["lat"], x["lon"], a, b) > 300 for a, b in seen)]
            pick = min(pool, default=None, key=lambda x: x["distance_m"] - 900 * bool(x.get("topic")) - 700 * bool(x.get("popular"))
                       - 500 * bool(x.get("because")) - 300 * bool(x.get("kids_ok") and trip.kids)
                       + 600 * (not x.get("description")))
            if not pick:
                break
            day["stops"].append(_fill_stop({"time": _hhmm(start), "kind": "sight", "ref": pick["ref"], "why": _why(pick),
                                            "added_by": "rihla"}, pick))
            used.add(_key(pick["name"]))
        day["stops"].sort(key=lambda st: _mins(st.get("time", "")) or 0)


def _untangle(final: dict) -> None:
    """No two stops at the same time: each stop starts when the previous one is over (prayers stay at their
    exact times; a visit that would start during a prayer starts after it)."""
    for day in final.get("days", []):
        stops = sorted(day["stops"], key=lambda st: (_mins(st.get("time", "")) or 0, st.get("kind") != "prayer"))
        free = 0
        for st in stops:
            t = _mins(st.get("time", "")) or 0
            if st.get("kind") == "prayer":
                free = max(free, t + DURATION["prayer"])
                continue
            if t < free:
                t = (free + 4) // 5 * 5
                st["time"] = _hhmm(t)
            free = t + DURATION.get(st.get("kind"), 45)
        day["stops"] = sorted(stops, key=lambda st: _mins(st.get("time", "")) or 0)


def _path_m(points: list[tuple[float, float]]) -> float:
    return sum(osm.distance_m(*points[i], *points[i + 1]) for i in range(len(points) - 1))


def _route(final: dict) -> None:
    """Walk less: the model often zigzags across a city. Within each day the sights are reordered into the
    shortest path (nearest next, starting where the day starts) and take over the model's time slots;
    meals and prayers keep their times."""
    for day in final.get("days", []):
        sights = [st for st in day["stops"] if st.get("kind") == "sight" and st.get("lat") is not None]
        if len(sights) < 3:
            continue
        slots = [st["time"] for st in sights]
        order, left = [sights[0]], sights[1:]
        while left:
            nxt = min(left, key=lambda st: osm.distance_m(order[-1]["lat"], order[-1]["lon"], st["lat"], st["lon"]))
            order.append(nxt)
            left.remove(nxt)
        pts = lambda lst: [(st["lat"], st["lon"]) for st in lst]
        if _path_m(pts(order)) < 0.8 * _path_m(pts(sights)):
            for st, t in zip(order, slots):
                st["time"] = t
            day["stops"].sort(key=lambda st: _mins(st.get("time", "")) or 0)


async def _meals_nearby(final: dict, trip: Trip) -> None:
    """Eat near where you are: a meal more than 2 km from the previous stop is swapped for an equally sure
    halal option within 1.2 km of it (the model tends to pick restaurants from the city centre)."""
    used = {_key(st.get("name", "")) for d in final.get("days", []) for st in d["stops"] if st.get("kind") != "prayer"}
    for day in final.get("days", []):
        for i, st in enumerate(day["stops"]):
            if st.get("kind") != "meal" or st.get("lat") is None:
                continue
            before = [x for x in day["stops"][:i][::-1] if x.get("kind") != "prayer" and x.get("lat") is not None]
            if not before:
                continue
            here = (before[0]["lat"], before[0]["lon"])
            if osm.distance_m(*here, st["lat"], st["lon"]) <= 2000:
                continue
            rank = RANK.get(st.get("halal_level"), 3)
            def options():
                return [x for x in trip.near("halal", *here, 1500, 30)
                        if _key(x["name"]) not in used and RANK.get(x.get("halal_level"), 3) <= rank]
            if not options():  # nothing looked up around here yet: ask Qloo and OpenStreetMap
                await _safe_tool(trip, "halal_food_near", {"lat": here[0], "lon": here[1], "radius_m": 1200})
            food = min(options(), key=lambda x: (RANK.get(x.get("halal_level"), 3), bool(x.get("alcohol")), x["distance_m"]), default=None)
            if food:
                used.discard(_key(st.get("name", "")))
                keep = {k: st[k] for k in ("time", "kind") if k in st}
                st.clear()
                st.update(_fill_stop(dict(keep, ref=food["ref"], added_by="rihla",
                                          why=f"A halal meal a short walk from {before[0].get('name', 'your previous stop')}."), food))
                used.add(_key(food["name"]))


def _polish(final: dict, trip: Trip) -> None:
    """Fix what the model tends to get wrong: places that match what the traveller said they love must be in
    the plan (e.g. the Natural History Museum for kids who love dinosaurs), and a 'why' that describes a
    different place is replaced by the place's own description."""
    days = final.get("days", [])
    if not days:
        return
    in_plan = {_key(st.get("name", "")) for d in days for st in d["stops"]}
    words = lambda x: {_singular(w) for k in x.get("topic") or [] for w in re.findall(r"[a-z]{4,}", k.lower())} & trip.topics
    musts = sorted((x for x in trip.places.values() if (x.get("topic") or x.get("requested")) and x.get("source") == "qloo"
                    and not x.get("halal_level") and x.get("kind") != "mosque" and x.get("lat") is not None),
                   key=lambda x: (not x.get("requested"), not x.get("popular"), x.get("distance_m") or 0))
    done = set().union(*[words(x) for x in musts if _key(x["name"]) in in_plan]) if musts else set()
    done |= {w for d in days for st in d["stops"] for w in words(st)}
    added = 0
    for x in musts:
        if added < 3 and _key(x["name"]) not in in_plan and (x.get("requested") or not words(x) <= done):
            day = min(days, key=lambda d: osm.distance_m(*_centre(d, (trip.dest["lat"], trip.dest["lon"])), x["lat"], x["lon"]))
            weak = [st for st in day["stops"] if st.get("kind") == "sight" and not st.get("topic") and not st.get("requested")]
            weak.sort(key=lambda st: (bool(st.get("because")), bool(st.get("popular"))))
            new = _fill_stop({"time": weak[0]["time"] if weak else "10:00", "kind": "sight", "ref": x["ref"], "why": _why(x),
                              "added_by": "rihla"}, x)
            if weak:
                day["stops"][day["stops"].index(weak[0])] = new
            else:
                day["stops"].append(new)
            day["stops"].sort(key=lambda st: _mins(st.get("time", "")) or 0)
            in_plan.add(_key(x["name"]))
            done |= words(x)
            added += 1
    _check_hours(final, trip)
    _fill_days(final, trip)
    _check_hours(final, trip, drop=False)
    # pray at the mosque nearest to where the family is at that time (the model tends to reuse one mosque all day)
    for d in days:
        for i, st in enumerate(d["stops"]):
            if st.get("kind") != "prayer":
                continue
            around = [x for x in d["stops"][i - 1::-1] + d["stops"][i + 1:] if x.get("kind") != "prayer" and x.get("lat") is not None] if i else                 [x for x in d["stops"][1:] if x.get("kind") != "prayer" and x.get("lat") is not None]
            if not around or st.get("lat") is None:
                continue
            here = (around[0]["lat"], around[0]["lon"])
            best = min(trip.near("mosques", *here, 6000, 12), default=None,
                       key=lambda m: m["distance_m"] - (400 if m.get("source") == "qloo" else 0))
            if best and best["ref"] != st.get("ref") and                     osm.distance_m(*here, st["lat"], st["lon"]) - best["distance_m"] > 600:
                keep = {k: st[k] for k in ("time", "kind", "prayer") if k in st}
                st.clear()
                st.update(_fill_stop(dict(keep, ref=best["ref"], why=f"{keep.get('prayer', 'Prayer')} at the mosque nearest to "
                                                                       f"{around[0].get('name', 'your previous stop')}."), best))
    _jumuah(final, trip)
    # a "why" that names another place (the model mixed two stops up) is replaced by the facts we have
    names = {_key(x.get("name", "")) for x in trip.places.values() if len(x.get("name", "")) >= 8}
    for d in days:
        for st in d["stops"]:
            why, own = _key(st.get("why", "")), _key(st.get("name", ""))
            mixed = own and own not in why and any(n != own and n not in own and why.startswith(n) for n in names)
            if st.get("kind") != "prayer" and (mixed or re.search(r"\bfans?\b", why)):
                st["why"] = _why(trip.places.get(st.get("ref"), st))


async def _enrich(final: dict, trip: Trip, prayers: list[dict]) -> dict:
    """Attach real coordinates and details to every stop; drop stops that reference unknown places."""
    last = (trip.dest["lat"], trip.dest["lon"])
    used: set[str] = set()
    used_names: set[str] = set()
    used_points: list[tuple[float, float]] = []  # the same museum can exist twice in the data under two names
    for i, day in enumerate(final.get("days", [])):
        day["date"] = prayers[i]["date"] if i < len(prayers) else None
        day["hijri"] = prayers[i]["hijri"] if i < len(prayers) else None
        day["prayer_times"] = prayers[i]["timings"] if i < len(prayers) else {}
        stops, prayed = [], set()
        for s in day.get("stops", []):
            p = trip.places.get(s.get("ref", ""))
            want = s.get("kind")
            is_food = bool(p and p.get("halal_level"))
            is_mosque = bool(p and p.get("kind") == "mosque")
            # the model sometimes files a sight under "meal" or a cafe under "prayer": fix it from the data
            if want == "prayer" and not is_mosque:
                p = next(iter(trip.near("mosques", *last, 6000, 1)), None)
                if p:
                    s["why"] = ""
            elif want == "meal" and not is_food:
                options = [x for x in trip.near("halal", *last, 2500, 30) if x["ref"] not in used and _key(x["name"]) not in used_names]
                p = min(options, key=lambda x: (RANK.get(x.get("halal_level"), 3), x["distance_m"]), default=None)
                if p:
                    s["why"] = "A halal option close to your previous stop."
            elif want == "sight" and (is_food or is_mosque):
                s["kind"] = "meal" if is_food else "prayer"
            if p:
                s["ref"] = p.get("ref", s.get("ref"))
            if s.get("kind") == "prayer" and day.get("prayer_times"):
                # a prayer stop sits exactly at the prayer time, whatever time the model wrote
                name = _prayer_name(s.get("time", ""), day["prayer_times"])
                if name in prayed:
                    continue  # one stop per prayer (the model sometimes lists two mosques for Dhuhr)
                prayed.add(name)
                s["time"], s["prayer"] = day["prayer_times"][name], name
                if not s.get("why") or "prayer" not in s["why"].lower():
                    s["why"] = f"{name} prayer at the mosque closest to your previous stop."
            if not p:
                continue
            if s.get("kind") not in ("sight", "meal", "prayer", "rest"):
                s["kind"] = "prayer" if p.get("kind") == "mosque" else "meal" if p.get("halal_level") else "sight"
            twin = s["kind"] == "sight" and p.get("lat") is not None and \
                any(osm.distance_m(p["lat"], p["lon"], a, b) < 80 for a, b in used_points)
            if s["kind"] != "prayer" and (s.get("ref") in used or _key(p.get("name", "")) in used_names or twin):
                # never the same sight or restaurant twice in one trip (not even another branch of a chain)
                if s["kind"] != "meal":
                    continue
                options = [x for x in trip.near("halal", *last, 2500, 30) if x["ref"] not in used and _key(x["name"]) not in used_names]
                p = min(options, key=lambda x: (RANK.get(x.get("halal_level"), 3), x["distance_m"]), default=None)
                if not p:
                    continue
                s["ref"], s["why"] = p["ref"], "A halal option close to your previous stop."
            used.add(s.get("ref"))
            if s["kind"] != "prayer":
                used_names.add(_key(p.get("name", "")))
            if s["kind"] == "sight" and p.get("lat") is not None:
                used_points.append((p["lat"], p["lon"]))
            _fill_stop(s, p)
            if p.get("lat"):
                last = (p["lat"], p["lon"])
            stops.append(s)
        day["stops"] = sorted(stops, key=lambda x: _mins(x.get("time", "")) or 0)
    _polish(final, trip)
    _route(final)
    _check_hours(final, trip, drop=False)
    await _meals_nearby(final, trip)
    _untangle(final)
    final["destination"] = trip.dest
    final["trace"] = trip.trace
    final["signals"] = list(trip.signals.values())
    final["audiences"] = ["Islam"] + (["Parents with young children"] if trip.kids else [])
    final["qloo_mode"] = "mock" if config.QLOO_MOCK else "live"
    final["model"] = trip.model
    final["llm_log"] = getattr(trip, "llm_log", [])
    return final
