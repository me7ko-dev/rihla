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
import logging

import asyncio
import re
import time
from difflib import SequenceMatcher

from openai import APIError, AsyncOpenAI, BadRequestError, RateLimitError

from . import config, halal, osm, prayer, qloo, wiki

log = logging.getLogger("rihla")
MAX_STEPS = 6
# Vercel stops the function at 300 s: the plan must be ready well before that, with or without the model
BUDGET_S = 240
COMPOSE_THINK = False  # Nemotron's reasoning on the final plan: ~70 s instead of ~20 s, little better in tests
AREA_M = 3000  # one OpenStreetMap area around the destination (seed_cities.py uses the same)
EMPTY_AREA = {"halal": [], "mosques": [], "sights": []}
KIDS = re.compile(r"\b(kids?|child|children|sons?|daughters?|bab(y|ies)|toddlers?|boys?|girls?|famil(y|ies)"
                  r"|деца|дете(то)?|бебе(та)?|син|дъщеря|семейство)\b", re.I)
BABY = re.compile(r"\b(bab(y|ies)|toddlers?|infants?|бебе(та)?)\b", re.I)
# "a 4-year-old", "6 years old", "aged 3", "4-годишно": a child's age even without a word like "kids"
AGE = re.compile(r"\b(\d{1,2})\s*-?\s*(?:years?|yrs?)\s*-?\s*olds?\b|\b(\d{1,2})\s*y/?o\b|\bage[ds]?\s*(?:of\s*)?(\d{1,2})\b"
                 r"|\b(\d{1,2})\s*-?\s*годишн", re.I)
RANK = {"verified": 0, "likely": 1, "unknown": 2}
# "Orhan Pamuk's novels" -> "Orhan Pamuk", "Pixar movies" -> "Pixar"
GENERIC_WORDS = re.compile(r"'s\b|\b(movies?|films?|novels?|books?|series|shows?|tv|music|songs?|albums?|video ?games?|games?|"
                           r"franchise|saga|cartoons?|anime)\b", re.I)
NOT_A_TASTE = {"place", "locality", "destination", "album"}  # untyped search hits that are rarely what people mean
STOP = set("""love loves loved like likes enjoy enjoys into also really very much with they them their our ours we us
and the are is a an of for to in on at our my his her kids kid children child family families son sons daughter
daughters wife husband aged age years old year who what all some any lot lots big fan fans favourite favorite
especially things stuff place places food spicy good great movies movie films film books book novels novel music
songs series shows show games game want wants wanted see seeing visit visiting trip travel city day days time tour
explore interested interest new old top best around near most more just like way lot get go going can will would
she he it its from this that these those""".split())
# Qloo also tags hotels, restaurants, shops and offices as "tourist attraction" or "historical landmark": a place to
# visit is not a place to sleep, eat, shop or book a tour. Some are fine when that is what they asked for (a bookstore).
NOT_A_SIGHT = re.compile(r"\b(tour operator|tour agency|travel agency|car rental|rental agency|ticket agency|hotel|hostel|motel|"
                         r"inn|ryokan|lodging|resort|guest house|bed and breakfast|apartment|serviced apartment|restaurant|"
                         r"steakhouse|bar|pub|cabaret|night club|storage|warehouse|movers|moving company|convention center|"
                         r"school|wedding venue|furniture|company|corporate office|office|bank|pharmacy|car dealer|"
                         r"manufacturer|golf driving range|subway station|metro station|bus station|bus stop|tram stop|"
                         r"light rail station|parking|residential)\b", re.I)
# a shop, a club's office or a station is a sight only when it is also an attraction (Platform 9¾, Grand Central)
SHOP = re.compile(r"\b(store|shop|boutique|mall|grocery|supermarket|charity|market|club|organization|association|"
                  r"non profit|station|event venue|banquet hall|dance hall)\b", re.I)
NOT_A_SIGHT_NAME = re.compile(r"\b(tours?|rentals?|travel agency|hostel|hotel)\b", re.I)  # "Toronto bicycle tour & events"
# long closures the data does not show yet (the place is listed with opening hours): never planned as a visit
CLOSED_FOR_YEARS = re.compile(r"^(the )?centre pompidou$|^ontario science centre$", re.I)
ATTRACTION = re.compile(r"museum|gallery|landmark|historical|monument|memorial|zoo|aquarium|park|garden|palace|castle|"
                        r"cathedral|church|mosque|temple|shrine|tourist attraction|observation|viewpoint|stadium|arena|library", re.I)
HOTEL_TEXT = re.compile(r"\b(suites|guest rooms|butler service|complimentary breakfast|check-in|boutique hotel)\b", re.I)
KIDS_PLACE = re.compile(r"children\W*s museum|childrens museum|playground|kids'? (?:club|zone|play)|soft play", re.I)
# not for young kids, whatever the taste data says
GRIM = re.compile(r"tortur|ripper|dungeon|horror|serial killer|execution|gallows|crime museum|\bsex|erotic|strip club|"
                  r"macabre|anatom|specimens|surgeon|surgical|cadaver|morbid|cabaret|burlesque", re.I)

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
- Never write that a restaurant is halal, certified or alcohol-free (or not): the app shows an honest halal label.
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
    # tips before days: written after the long days array, the model often lost them in the closing brackets
    "tips": {"type": "array", "items": {"type": "string"}},
    "days": {"type": "array", "items": {"type": "object", "properties": {
        "day": {"type": "integer"}, "theme": {"type": "string"},
        "stops": {"type": "array", "items": {"type": "object", "properties": {
            "time": {"type": "string", "description": "HH:MM"},
            "kind": {"type": "string", "description": "one of: sight, meal, prayer, rest"},
            "ref": {"type": "string", "description": "exact ref from a tool result"},
            "why": {"type": "string"}},
            "required": ["time", "kind", "ref", "why"]}}},
        "required": ["day", "theme", "stops"]}}},
    "required": ["title", "summary", "days"]}
COMPOSE = ("You have gathered enough. Now write the final itinerary as ONE JSON object, no prose, with this shape: "
           + json.dumps(PLAN_SCHEMA) + " Use only refs you received from tools. Write all text in {language}.")


class LLM:
    """Primary model with a fallback provider; retries briefly on rate limits."""

    def __init__(self, deadline: float | None = None):
        # Nemotron: turning the visible "thinking" off makes each call ~2 s instead of 5-30 s
        nv = {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}} if "nvidia" in config.LLM_BASE_URL else {}
        # no hidden SDK retries (3 x 150 s would outlast the server): a slow provider hands over to the next one
        self.clients = []
        for key, url, model, limit, extra in ((config.LLM_API_KEY, config.LLM_BASE_URL, config.LLM_MODEL, 150, nv),
                                              (config.FALLBACK_API_KEY, config.FALLBACK_BASE_URL, config.FALLBACK_MODEL, 90, {})):
            if key:
                self.clients.append((AsyncOpenAI(api_key=key, base_url=url, timeout=limit, max_retries=0), model, extra, limit))
        self.deadline = deadline or time.time() + BUDGET_S
        self.used = config.LLM_MODEL
        self.log: list[dict] = []

    def left(self) -> float:
        return self.deadline - time.time()

    async def chat(self, think: bool = False, **kw):
        """think=True keeps Nemotron's reasoning on (slower, better) — used for the final itinerary."""
        last: Exception | None = None
        for client, model, extra, limit in self.clients:
            if think:
                extra = {}
            for attempt in range(3):
                wait = min(limit, self.left() - 20)
                if wait < 10:
                    raise RuntimeError("No time left for the language model")
                try:
                    t0 = time.time()
                    r = await client.chat.completions.create(model=model, **kw, **extra, timeout=wait)
                    self.used = model
                    self.log.append({"model": model, "s": round(time.time() - t0, 1),
                                     "out": getattr(r.usage, "completion_tokens", None), "think": not extra})
                    return r
                except BadRequestError:
                    raise
                except RateLimitError as e:
                    last = e
                    if re.search(r"per day|TPD|RPD|daily", str(e), re.I):
                        break  # a daily limit: waiting a few seconds will not help, the next provider might
                    await asyncio.sleep(4 * (attempt + 1))
                except APIError as e:  # 5xx / timeouts: try the next provider
                    last = e
                    break
        raise RuntimeError(f"All language models failed: {last or 'no API key'}")


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


def _usable(final, days: int) -> bool:
    """Keep only well-formed days and stops (the model sometimes slips a stray word into an array, like
    '"days": [{...}, "tips"]') and say whether every day of the trip is still there, each with stops."""
    if not isinstance(final, dict) or not isinstance(final.get("days"), list):
        return False
    final["days"] = [d for d in final["days"] if isinstance(d, dict) and isinstance(d.get("stops"), list)][:days]
    for d in final["days"]:
        d["stops"] = [s for s in d["stops"] if isinstance(s, dict)]
    tips = final.get("tips")
    if not isinstance(tips, list):  # the model sometimes files the tips inside a day, or writes them as one text
        tips = next((d.pop("tips") for d in final["days"] if isinstance(d.get("tips"), list)), tips)
    if isinstance(tips, str):
        tips = [tips]
    if isinstance(tips, list):  # one text with several tips: one per line ("; " joins two halves of one tip)
        tips = [t for x in tips if isinstance(x, str) for t in re.split(r"\s*\n\s*", x)]
    final["tips"] = [t.strip() for t in tips if isinstance(t, str) and len(re.findall(r"\w", t)) >= 3] if isinstance(tips, list) else []
    return len(final["days"]) == days and all(d["stops"] for d in final["days"])


class Trip:
    """State of one planning run: the places the tools returned (by ref), the traveller's Qloo taste
    signals, and a trace for the UI."""

    def __init__(self, dest: dict, travellers: str = "", tastes: str = "", typed: str = "", language: str = "English"):
        self.dest = dest
        self.language = language or "English"
        # the destination itself ("Sofia, Bulgaria") is never a stop, and its name is never a topic
        self.typed = typed or str(dest.get("name", "")).split(",")[0]
        self.dest_keys = {k for k in (_key(typed), _key(typed.split(",")[0]), _key(str(dest.get("name", "")).split(",")[0])) if k}
        # words the traveller used ("dinosaurs", "street art"): matched against what reviewers say a place is known for
        self.topics = _words(tastes) - {w for k in self.dest_keys for w in _words(k)}
        self.named: set[str] = set()  # words of the names they wrote that Qloo knows ("Harry Potter")
        self.tastes = tastes or ""
        self.places: dict[str, dict] = {}
        self.pool: dict[str, dict[str, dict]] = {"halal": {}, "mosques": {}, "sights": {}}
        self.trace: list[dict] = []
        self.model = ""
        self.entities: dict[str, dict] = {}  # every Qloo entity seen in taste_entities, by id
        self.signals: dict[str, str] = {}    # the traveller's taste signals: Qloo id -> name
        ages = [int(a) for a in re.findall(r"\b(\d{1,2})\b", travellers or "")]
        stated = [int(next(g for g in m if g)) for m in AGE.findall(travellers or "")]
        self.kids = bool(KIDS.search(travellers or "")) and (not ages or min(ages) <= 12) or \
            bool(BABY.search(travellers or "")) or any(a <= 12 for a in stated)
        self.audiences = [qloo.AUDIENCES["muslim"]] + ([qloo.AUDIENCES["kids"]] if self.kids else [])
        self.tags: dict[str, str] | None = None  # Qloo tags for the topics they wrote: tag id -> the word as written
        # extra searches after the model, each kind with its own budget (mosques near a stop, food near a meal, sights
        # near a day that ran out of them)
        self.lookups = {"mosques": 4, "halal": 4, "sights": 3}
        self.osm_task: asyncio.Task | None = None
        self._osm: dict | None = None
        self._osm_by: float | None = None

    async def osm(self, wait: float = 20.0) -> dict:
        """The OpenStreetMap area, fetched in the background; empty if it is not ready in time
        (the public Overpass server takes 20-140 s for a new city)."""
        if self._osm is not None:
            return self._osm
        if self.osm_task is None:
            return EMPTY_AREA
        # the first caller waits; once that wait is over, later callers only take the map if it has arrived
        if self._osm_by is None:
            self._osm_by = time.time() + wait
        wait = max(0.05, min(wait, self._osm_by - time.time()))
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
        old = self.places.get(ref)
        if old:  # the same place again from another tool: what we already know about it stays (asked for, a topic...)
            for k in ("requested", "popular"):
                p[k] = bool(p.get(k) or old.get(k))
            for k in ("topic", "because"):
                p[k] = list(dict.fromkeys((p.get(k) or []) + (old.get(k) or [])))[:2]
        self.places[ref] = p
        for name, entries in self.pool.items():
            if ref in entries:
                entries[ref] = p if name == pool or pool is None else entries[ref]
        if pool and p.get("lat") is not None and ref not in self.pool[pool]:
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
                if kind == "halal" and qloo.is_alcohol_venue(p):  # a meyhane or a bar mapped as a restaurant
                    continue
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


# words that say nothing about a topic ("New York Knicks" is about York and the Knicks) — unlike "food" in "street food"
TINY = {"the", "and", "for", "with", "new", "old", "top", "best", "big", "great", "good", "most", "more", "san", "st"}
# words a reviewers' keyword may add to their topic and still be about it: "dinosaur fossils", "calligraphy exhibit"
KEYWORD_EXTRAS = {"fossil", "skeleton", "exhibit", "exhibition", "display", "collection", "gallery", "room", "hall",
                  "section", "model", "replica"}


def _words(text: str) -> set[str]:
    """The meaningful words of a text, singular, without accents ("Dinosaurs" -> "dinosaur", "Pokémon" -> "pokemon")."""
    return {_singular(w) for w in re.findall(r"[^\W\d_]{3,}", halal._norm(text or "")) if w not in STOP}


def _topic_hits(trip: Trip, it: dict) -> list[str]:
    """What the place is known for that is exactly what they wrote: a reviewers' keyword all of whose words they used
    ("dinosaurs"; "street art" but not "street food"; "Harry Potter" at Leadenhall Market), or a Qloo tag of their topic."""
    mine = trip.topics | trip.named
    every = lambda k: {_singular(w) for w in re.findall(r"[^\W\d_]{3,}", halal._norm(k))} - TINY
    hits = [_as_written(trip.tastes, k) for k in it.get("known_for") or []
            if every(k) & mine and every(k) <= mine | KEYWORD_EXTRAS]
    hits = list(dict.fromkeys(hits + [trip.tags[t] for t in it.get("tag_ids") or [] if t in (trip.tags or {})]))
    # one topic once: "dinosaurs" covers "dinosaur skeletons"
    core = lambda h: every(h) & mine
    out: list[str] = []
    for h in sorted(hits, key=lambda h: len(every(h))):  # the plainest words first
        if not any(core(o) <= core(h) for o in out):
            out.append(h)
    return out[:2]


def _not_a_sight(it: dict, trip: Trip | None = None, wanted: set[str] | None = None) -> bool:
    """A hotel, restaurant, shop, office or the city itself — not a place to visit (unless it is what they asked
    for: a bookstore for readers, a cafe). Kids-only places are not for adults; grim ones not for children."""
    wanted = wanted or set()
    cats = [c for c in it.get("categories") or [] if c not in wanted]
    if it.get("lodging") or it.get("closed_for_good"):
        return True
    # "suites" in the text of a hotel; the Burj Khalifa has suites too, but it is the world's tallest building
    if HOTEL_TEXT.search(it.get("description") or "") and not (float(it.get("popularity") or 0) >= 0.99 or it.get("requested")):
        return True
    if any(NOT_A_SIGHT.search(c) and not ("cafe" in wanted and "restaurant" in c) for c in cats):
        return True
    attraction = any(ATTRACTION.search(c) for c in it.get("categories") or [])
    if any(SHOP.search(c) for c in cats) and not attraction:
        return True
    if (NOT_A_SIGHT_NAME.search(it.get("name") or "") and not attraction) or CLOSED_FOR_YEARS.search((it.get("name") or "").strip()):
        return True
    if trip is not None:
        if _key(it.get("name", "")) in trip.dest_keys:
            return True
        text = " ".join(it.get("categories") or []) + " " + (it.get("name") or "")
        if not trip.kids and KIDS_PLACE.search(text):
            return True
        if trip.kids and GRIM.search((it.get("name") or "") + " " + (it.get("description") or "")):
            return True
    return False


NOT_FOOD = re.compile(r"\b(grocery|supermarket|hypermarket|convenience store|department store|shopping mall|butcher|"
                      r"golf|driving range|gas station|pharmacy)\b", re.I)


def _not_food(it: dict) -> bool:
    """A supermarket with a halal section, a butcher or a golf range is not a place for a meal."""
    cats = it.get("categories") or []
    return bool(cats) and any(NOT_FOOD.search(c) for c in cats) and not any(re.search(r"restaurant|cafe|food", c) for c in cats) \
        or bool(re.search(r"\b(sainsbury'?s|tesco|carrefour|lidl|aldi|walmart|costco|spar|7-eleven|migros|bim|a101|şok)\b",
                          it.get("name") or "", re.I))


def _qplace(it: dict, kind: str, trip: Trip, lat: float, lon: float, because: list[str] | None = None) -> dict:
    """A Qloo place as Rihla's place dict; 'because' = the traveller's tastes it matches."""
    because = because or []
    hits = _topic_hits(trip, it)
    p = {k: it.get(k) for k in ("name", "lat", "lon", "address", "image", "website", "rating", "categories", "affinity",
                                "description", "known_for", "hours", "lodging", "closed_for_good", "popularity")}
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


def _variants(q: str) -> list[str]:
    """Shorter forms to try when the full text finds nothing: "Orhan Pamuk'un romanları" -> "Orhan Pamuk" (Turkish and
    other languages attach endings with an apostrophe), then the first two words."""
    out = [q]
    bare = " ".join(re.sub(r"['’][^\s]*", "", q).split())
    if bare and bare not in out:
        out.append(bare)
    words = bare.split()
    if len(words) > 2:
        out.append(" ".join(words[:2]))
    return out


async def _lookup(trip: Trip, query: str, kind: str | None) -> tuple[list, str]:
    for q in _variants(" ".join(GENERIC_WORDS.sub(" ", query).split()) or query):
        found, note = await _lookup_one(trip, q, query, kind)
        if found:
            return found, note
    return [], "no match"


async def _lookup_one(trip: Trip, q: str, query: str, kind: str | None) -> tuple[list, str]:
    """One favourite -> Qloo entities; the best match becomes one of the traveller's taste signals.
    Score = how well the name matches + the requested type + Qloo popularity, so "Harry Potter" (a book)
    beats the unknown TV show of the same name, and "Marvel movies" never becomes a singer called Märvel."""
    kind = kind if kind in qloo.TYPES else None
    key, want = _key(q), qloo.TYPES.get(kind or "", "")
    res = await asyncio.gather(qloo.search(q, None, take=8), qloo.search(q, kind, take=5) if kind else asyncio.sleep(0, []),
                               return_exceptions=True)
    cands, places = {}, []
    for e in [x for r in res if isinstance(r, list) for x in r]:
        if str(e["type"]).rsplit(":", 1)[-1] not in NOT_A_TASTE:
            cands.setdefault(e["id"], e)
        elif e["type"] == "urn:entity:place" and e.get("lat") is not None and \
                osm.distance_m(e["lat"], e["lon"], trip.dest["lat"], trip.dest["lon"]) < 40000 and \
                (_key(e["name"]) == key or re.search(r"\b" + re.escape(key) + r"\b", _key(e["name"]))) and \
                not _not_a_sight(dict(e, requested=_key(e["name"]) == key), trip):
            # "Hertha BSC" the club shop, or "Burj Khalifa/ Dubai Mall Metro Station", is not what they asked to visit
            places.append(e)
    if places:  # they want to go there: a must-visit stop
        # "Louvre": the museum rather than its glass pyramid (equally popular, so the museum category decides)
        it = max(places, key=lambda e: (_key(e["name"]) == key, round(float(e.get("popularity") or 0), 2),
                                        any(ATTRACTION.search(c) and "tourist attraction" not in c for c in e.get("categories") or [])))
        p = _qplace(it, "landmark", trip, trip.dest["lat"], trip.dest["lon"])
        p["requested"] = True
        # "Burj Khalifa" is one place: its words must not match other places' keywords ("Burj al Arab")
        trip.topics -= _words(key)
        ref = "qloo:" + str(it["id"])
        trip.keep(ref, p, "qloo", "sights")
        return [{"ref": ref, "name": it["name"], "type": "place",
                 "note": "a place the traveller wants to visit: include it in the plan"}], f"{it['name']} (a place to visit)"

    podcasts = "podcast" in trip.tastes.lower()

    def score(e: dict) -> float:
        name = _key(e["name"])
        whole = bool(key) and re.search(r"\b" + re.escape(key) + r"\b", name)  # "marvel" is not in "marvelous"
        match = 1.0 if name == key else 0.7 if whole else 0.3 if SequenceMatcher(None, key, name).ratio() >= 0.8 else 0
        # "Ottoman history" is a topic, not the "Ottoman History Podcast": a podcast only if they wrote about one
        if not match or (e["type"] == "urn:entity:podcast" and not podcasts and want != "urn:entity:podcast"):
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
    # "Toronto Maple Leafs" is a team: "maple" or "leaf" must not make a park "known for" what they love
    trip.named |= _words(q)
    trip.topics -= _words(q)
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


TOPIC_TAGS = 4  # topics looked up in Qloo's tags per trip


async def _topic_tags(trip: Trip) -> dict[str, str]:
    """The topics they wrote ("dinosaurs", "calligraphy", "Ottoman history") as Qloo tags, so places are ranked by
    them on Qloo's side too. A tag counts only if its name is the word they wrote (no loose semantic neighbours)."""
    if trip.tags is not None:
        return trip.tags
    text = halal._norm(trip.tastes)
    named = trip.named | {w for n in trip.signals.values() for w in _words(n)}  # already taste signals
    words = sorted(trip.topics - named, key=lambda w: (text.find(w) if w in text else len(text)))[:TOPIC_TAGS]
    found = await asyncio.gather(*[qloo.find_tags(w, take=8, places=True) for w in words], return_exceptions=True)
    trip.tags = {}
    for w, tags in zip(words, found):
        m = re.search(r"\b" + re.escape(w) + r"\w*", trip.tastes, re.I)  # "dinosaur" -> "dinosaurs", as they wrote it
        written = m.group(0) if m else w
        for t in tags if isinstance(tags, list) else []:
            name = str(t.get("name") or "").lower()
            if t.get("id") and w in _words(name):
                trip.tags[t["id"]] = written
    if trip.tags:
        trip.trace.append({"tool": "topic_tags", "args": {"topics": words}, "found": len(trip.tags),
                           "note": "Qloo tags used as taste signals: " + ", ".join(dict.fromkeys(trip.tags.values()))})
    return trip.tags


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
    solo = refs[:5]  # each taste on its own: the honest basis for "because you love X"
    topic_ids = list(await _topic_tags(trip))
    wanted = {t.rsplit(":", 1)[-1].replace("_", " ") for t in union}

    async def ranked():  # with their topics as tag signals; as before if Qloo refuses them
        try:
            return await qloo.insights("place", interests=refs, tags=union, take=50, signal_tags=topic_ids, **ask)
        except qloo.QlooError:
            if not topic_ids:
                raise
            log.warning("Qloo refused the topic tags %s", topic_ids)
            return await qloo.insights("place", interests=refs, tags=union, take=50, **ask)
    res = await asyncio.gather(ranked(),
                               qloo.insights("place", tags=union, take=30, lat=lat, lon=lon, radius_m=radius,
                                             exclude_tags=avoid),  # the city's best-loved places: no taste, no audience
                               *[qloo.insights("place", interests=[r], tags=union, take=20, **ask) for r in solo],
                               # the same audiences without any taste: what a taste adds is measured against this
                               qloo.insights("place", tags=union, take=30, **ask),
                               return_exceptions=True)
    ok = lambda it: it.get("lat") is not None and not _not_a_sight(it, trip, wanted)
    together, famous = [[it for it in x if ok(it)] if isinstance(x, list) else [] for x in res[:2]]
    combined = [[it for it in together if set(it["tag_ids"]) & set(qloo.CATEGORIES[c])] for c in cats]
    own = {r: [it for it in x if ok(it)] if isinstance(x, list) else [] for r, x in zip(solo, res[2:-1])}
    baseline = [it for it in res[-1] if ok(it)] if isinstance(res[-1], list) else famous
    popular = {it["id"] for it in famous[:12]}
    everything = {it["id"]: it for lst in [together, famous, *own.values()] for it in lst}

    def topic(it: dict) -> bool:
        return bool(_topic_hits(trip, it))
    # "Because you love X" only where X itself lifts the place: in X's own top 10 and at least 5 places higher than in
    # the same query without any taste. In a small city every taste's top 10 is the same famous ten — no badge there.
    base = {it["id"]: i for i, it in enumerate(baseline)}
    lifted = {r: {it["id"]: i for i, it in enumerate(items[:10]) if base.get(it["id"], 99) - i >= 5} for r, items in own.items()}

    def because(it: dict) -> list[str]:
        if OUTDOOR.search(" ".join(it.get("categories") or [])) and not topic(it):
            return []  # a park or a square is not "because you love Pixar"
        best = min(((ids[it["id"]], r) for r, ids in lifted.items() if it["id"] in ids), default=None)
        return [trip.signals[best[1]]] if best else []

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
            p = _qplace(it, c, trip, lat, lon, because(it))
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
        muslim_country = cc in halal.MUSLIM_MAJORITY
        tags = qloo.RESTAURANT if muslim_country else (qloo.HALAL_CUISINE,)
        radius = max(radius, 2000)
        # there, restaurants that serve alcohol are the exception: ask for the others first
        no_drinks = tuple(qloo.SERVES_ALCOHOL) if muslim_country else ()
        ask = dict(interests=list(trip.signals), audiences=trip.audiences, lat=lat, lon=lon, radius_m=3000, tags=tags, take=25)
        items, area = await asyncio.gather(
            qloo.insights("place", exclude_tags=qloo._cat("hotel", "lodging") + no_drinks, **ask),
            trip.osm(15), return_exceptions=True)
        items = items if isinstance(items, list) else []
        if muslim_country and len(items) < 8:
            try:
                more = await qloo.insights("place", exclude_tags=qloo._cat("hotel", "lodging"), **ask)
                items += [x for x in more if x["id"] not in {i["id"] for i in items}]
            except Exception:
                pass
        area = area if isinstance(area, dict) else EMPTY_AREA
        found = []
        for it in items:
            if it.get("lat") is None or it.get("closed_for_good") or it.get("lodging") or _not_food(it):
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
                   for it in items if it.get("lat") is not None and not _not_a_sight(it, trip)]
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
    try:
        dest = await osm.geocode(destination)
    except Exception:  # OpenStreetMap's geocoder can refuse cloud servers: ask Qloo instead
        dest = None
    if not dest:
        try:
            dest = await qloo.locate(destination)
        except Exception:
            dest = None
    if not dest:
        raise ValueError(f"Could not find '{destination}' on the map")
    trip = Trip(dest, travellers, tastes, destination, language)
    # OpenStreetMap can be slow for a new city: fetch it in the background while the agent works with Qloo
    trip.osm_task = asyncio.create_task(osm.area(dest["lat"], dest["lon"], AREA_M,
                                                 all_food=dest["country_code"] in halal.MUSLIM_MAJORITY))
    trip.osm_task.add_done_callback(lambda t: t.cancelled() or t.exception())  # an Overpass failure is expected, not news
    days = max(1, min(days, 4))
    await emit({"type": "step", "text": f"Getting prayer times for {days} day{'s' if days > 1 else ''}"})
    prayers = await asyncio.gather(*[_prayer_times(dest, start + dt.timedelta(days=i)) for i in range(days)])
    user = {
        "destination": {"name": dest["name"], "lat": dest["lat"], "lon": dest["lon"], "country_code": dest["country_code"]},
        "days": days, "start_date": start.isoformat(), "travellers": travellers, "tastes": tastes,
        "prayer_times": [{"day": i + 1, "date": p["date"], **p["timings"]} for i, p in enumerate(prayers)],
    }
    llm = LLM(deadline=t0 + BUDGET_S)
    msgs = [{"role": "system", "content": SYSTEM.replace("{language}", language)},
            {"role": "user", "content": "Plan this trip:\n" + json.dumps(user, ensure_ascii=False)}]
    # 1) research: the model calls the tools it needs until it says it is ready
    model_down = False
    for step in range(MAX_STEPS):
        if llm.left() < 120:
            break
        try:
            r = await llm.chat(messages=msgs, tools=TOOLS, temperature=0.4, max_tokens=4000, tool_choice="auto")
        except BadRequestError as e:
            # Groq rejects a malformed tool call outright; tell the model and let it try again
            if "tool_use_failed" not in str(e):
                model_down = True
                break
            msgs.append({"role": "user", "content": "Your last tool call had invalid arguments: " + str(e)[:300] + " Try again."})
            continue
        except RuntimeError:  # every language model failed
            model_down = True
            break
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
    if model_down and not trip.signals and not trip.pool["sights"]:
        await _research_without_model(trip, tastes, emit)
    # 2) names they wrote that the model did not look up ("We want to see the Burj Khalifa"), then halal food and a
    # mosque in every day's area, even if the model skipped those tools
    extra = await _missed_names(trip, tastes, emit) if not model_down else {}
    extra.update(await _fill_gaps(trip, days, emit))
    if not trip.places:
        raise RuntimeError("No places were found for this destination")
    # 3) compose: one JSON answer (more robust than a huge tool call)
    await emit({"type": "step", "text": "Building your day-by-day plan around the prayer times"})
    msgs.append({"role": "user", "content": (("More places I looked up for you: " + _fit(extra, 12000) + "\n\n")
                                             if extra else "") + COMPOSE.replace("{language}", language)})
    final = None
    for _ in range(0 if model_down else 2):
        try:
            r = await llm.chat(think=COMPOSE_THINK, messages=msgs, temperature=0.3, max_tokens=16000,
                               response_format={"type": "json_object"})
        except (RuntimeError, BadRequestError):
            break
        final = _json(r.choices[0].message.content or "")
        if _usable(final, days):
            break
        final = None
        msgs.append({"role": "user", "content": f"That was not valid JSON with a days array of {days} day object(s), "
                                                "each with its stops. Output only the JSON."})
    if final:
        trip.trace.append({"tool": "compose_plan", "args": {}, "found": sum(len(d["stops"]) for d in final["days"]),
                           "note": "itinerary written, every stop checked against the tool results"})
    else:
        final = _plan_without_model(trip, days, start)
        final["fallback"] = True
        trip.trace.append({"tool": "compose_plan", "args": {}, "found": sum(len(d["stops"]) for d in final["days"]),
                           "note": "the AI writer was unavailable: Rihla assembled the plan from the same Qloo results"})
    trip.model = llm.used
    trip.llm_log = llm.log
    trip.absorb_osm(await trip.osm(0.05))  # more mosques and halal food for the checks below, if the map is ready
    try:
        result = await _enrich(final, trip, prayers)
    except Exception:
        if final.get("fallback"):
            raise
        # whatever the model wrote, the traveller still gets a plan
        log.exception("the model's plan could not be used")
        final = _plan_without_model(trip, days, start)
        final["fallback"] = True
        trip.trace.append({"tool": "compose_plan", "args": {}, "found": sum(len(d["stops"]) for d in final["days"]),
                           "note": "the AI writer's plan could not be used: Rihla assembled the plan from the same Qloo results"})
        result = await _enrich(final, trip, prayers)
    if not model_down:
        await emit({"type": "step", "text": "Checking the titles and texts against the final plan"})
        await _edit_texts(result, trip, llm, travellers, tastes)
    await _wiki_photos(result)
    result["seconds"] = round(time.time() - t0)
    result["qloo_calls"] = {k: qloo.stats[k] - q0[k] for k in q0}
    return result


async def _prayer_times(dest: dict, date: dt.date) -> dict:
    """The day's prayer times; if AlAdhan cannot be reached (twice), the day is planned without them."""
    for attempt in range(2):
        try:
            return await prayer.times(dest["lat"], dest["lon"], date, dest["country_code"])
        except Exception:
            log.warning("prayer times unavailable for %s (attempt %d)", date, attempt + 1)
    return {"date": date.isoformat(), "hijri": "", "timings": {}, "method": "", "timezone": "", "missing": True}


def _written_names(tastes: str) -> list[str]:
    """Names the traveller wrote with capitals mid-sentence: "Orhan Pamuk", "Burj Khalifa", "Minecraft"."""
    return list(dict.fromkeys(m.strip(" ,.;") for m in
                              re.findall(r"(?<![.!?]\s)(?<!^)\b([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+)*)", tastes or "")))


async def _missed_names(trip: Trip, tastes: str, emit) -> dict:
    """Names of two or more words that the model did not look up (it sometimes sends only "Frozen" and forgets
    "Burj Khalifa"): Rihla looks them up itself, so a place they want to see is never lost."""
    asked = [_key(str(x.get("query", ""))) for t in trip.trace if t["tool"] == "taste_entities"
             for x in (t["args"].get("items") or []) if isinstance(x, dict)]
    names = [n for n in _written_names(tastes) if len(n.split()) >= 2 and _key(n) not in trip.dest_keys
             and not any(_key(n) in a or a in _key(n) for a in asked if a)][:4]
    if not names:
        return {}
    args = {"items": [{"query": n, "kind": None} for n in names]}
    await emit({"type": "step", "id": "missed", "text": _step_text("taste_entities", args)})
    result, note = await _safe_tool(trip, "taste_entities", args)
    trip.trace.append({"tool": "taste_entities", "args": args, "found": _count(result), "note": note + " (names the model skipped)"})
    await emit({"type": "found", "id": "missed", "n": _count(result)})
    return {"taste_entities for names you did not look up": result}


async def _research_without_model(trip: Trip, tastes: str, emit) -> None:
    """The language model is down: run the research tools directly — names the traveller wrote become taste
    signals, then places, halal food and mosques around the destination."""
    names = _written_names(tastes)
    steps = [("taste_entities", {"items": [{"query": n, "kind": None} for n in names[:6]]})] if names else []
    steps.append(("taste_places", {"categories": ["museum", "landmark", "park"] + (["family"] if trip.kids else []),
                                   "interest_refs": []}))
    for i, (name, args) in enumerate(steps):
        await emit({"type": "step", "id": f"auto{i}", "text": _step_text(name, args)})
        result, note = await _safe_tool(trip, name, args)
        trip.trace.append({"tool": name, "args": args, "found": _count(result), "note": note + " (run by Rihla)"})
        await emit({"type": "found", "id": f"auto{i}", "n": _count(result)})


def _plan_without_model(trip: Trip, days: int, start: dt.date) -> dict:
    """The language model could not write the plan: the code picks the best places per day from the same
    tool results (asked-for places and topic matches first, then taste matches and famous highlights),
    grouped by area; meals and prayers are added by the usual checks."""
    def score(x: dict) -> float:
        return (3 * bool(x.get("requested")) + 2 * bool(x.get("topic")) + bool(x.get("because")) + bool(x.get("popular"))
                + 0.5 * bool(trip.kids and x.get("kids_ok")) + 0.3 * bool(x.get("description")))
    cands = sorted((x for x in trip.pool["sights"].values() if not x.get("halal_level")), key=score, reverse=True)
    per_day = 3 if trip.kids else 4
    picked, spots = [], []
    for x in cands:  # skip places inside one already picked
        if all(osm.distance_m(x["lat"], x["lon"], a, b) > 250 for a, b in spots):
            picked.append(x)
            spots.append((x["lat"], x["lon"]))
        if len(picked) >= per_day * days:
            break
    centres = _centres([(x["lat"], x["lon"]) for x in picked], days) or [(trip.dest["lat"], trip.dest["lon"])]
    groups: list[list[dict]] = [[] for _ in centres]
    for x in picked:
        i = min(range(len(centres)), key=lambda i: osm.distance_m(x["lat"], x["lon"], *centres[i]))
        groups[i].append(x)
    city = trip.typed.split(",")[0].strip() or trip.dest.get("name", "").split(",")[0]
    out = {"title": f"{days} day{'s' if days > 1 else ''} in {city}", "tips": [],
           "summary": f"Places in {city} that match your taste, halal food nearby and every day built around the prayers."}
    out["days"] = []
    for i in range(days):
        date = (start + dt.timedelta(days=i)).isoformat()
        group = sorted((x for x in (groups[i] if i < len(groups) else []) if _opening(x, date) is not False),
                       key=score, reverse=True)[:per_day]
        slots = ["09:30", "11:00", "15:00", "16:30"]
        out["days"].append({"day": i + 1, "theme": f"{group[0]['name']} and around" if group else city,
                            "stops": [{"time": t, "kind": "sight", "ref": x["ref"], "why": _why(x)} for t, x in zip(slots, group)]})
    return out


_TIME = re.compile(r"(\d{1,2})\s*[:.h]\s*(\d{2})\s*([ap])?\.?\s*m?\b", re.I)


def _mins(hhmm: str) -> int | None:
    """Minutes after midnight for "13:30", "9:30", "1:30 PM" or "13h30"; None if it is not a time."""
    m = _TIME.search(hhmm) if isinstance(hhmm, str) else None
    if not m:
        return None
    h, mins, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ap == "p" and h < 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    return h * 60 + mins if h < 24 and mins < 60 else None


def _prayer_name(hhmm: str, timings: dict) -> str:
    """The daytime prayer (Dhuhr, Asr, Maghrib) closest to hh:mm."""
    t = _mins(hhmm)
    if t is None:
        return "Prayer"
    return min(("Dhuhr", "Asr", "Maghrib"), key=lambda p: abs((_mins(timings.get(p, "")) or 9999) - t))


STOP_FIELDS = ("name", "lat", "lon", "address", "cuisine", "halal_level", "halal_reason", "website", "image", "source",
               "affinity", "because", "categories", "rating", "alcohol", "description", "kids_ok", "topic", "popular",
               "open_today", "requested", "unnamed")


def _fill_stop(s: dict, p: dict) -> dict:
    s.update({k: p.get(k) for k in STOP_FIELDS if p.get(k) not in (None, "", [])})
    s["place_kind"] = p.get("kind", "")
    return s


def _short(text: str, n: int = 140) -> str:
    """At most n characters, cut at the end of a sentence when there is one (never in the middle of a thought)."""
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    cut = max(text.rfind(". ", 0, n), text.rfind("! ", 0, n), text.rfind("? ", 0, n))
    return text[:cut + 1] if cut >= min(40, n // 3) else text[:n].rsplit(" ", 1)[0].rstrip(",;:") + "…"


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
MIN_STAY = 30  # a visit with less time than this before a prayer (and the way to the mosque) starts after the prayer
JUMUAH_EARLY = 15  # arrive early for the Friday sermon
LATEST = 21 * 60 + 30  # nothing new starts after 21:30
MEAL_GAP = 150  # a second meal less than 2.5 hours after the first is one meal too many
# parks, gardens, fountains, viewpoints: places to see in daylight
OUTDOOR = re.compile(r"\b(park|garden|gardens|zoo|playground|fountain|square|bridge|viewpoint|scenic spot|scenic|lookout|"
                     r"mirador|beach|promenade|pier|walk)\b", re.I)
INDOOR = re.compile(r"museum|gallery|stadium|arena|mall|theat|aquarium|library|cinema", re.I)  # Madison Square Garden
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# a burger chain is never the taste-matched meal Rihla promises (it may still be the only halal option nearby)
CHAINS = re.compile(r"\b(mcdonald'?s|burger king|kfc|subway|starbucks|domino'?s|pizza hut|dunkin'?|popeyes|taco bell|"
                    r"wendy'?s|papa john'?s|little caesars|tim hortons|costa coffee|five guys|hardee'?s|carl'?s jr|jollibee|"
                    r"texas chicken|marrybrown|wingstop|panda express|dairy queen)\b", re.I)


def _opening(p: dict, date: str | None) -> tuple[int, int] | None | bool:
    """Qloo opening hours of a place on that date: (opens, closes) in minutes, False if closed, None if unknown."""
    hours = p.get("hours") if isinstance(p.get("hours"), dict) else {}
    try:
        slots = hours.get(WEEKDAYS[dt.date.fromisoformat(date).weekday()])
    except (TypeError, ValueError):
        return None
    if not slots:
        return None
    # Qloo marks some places closed every day of the week (the East Side Gallery, the Latin Bridge): that is
    # missing data, not a place that never opens
    if all(all(x.get("closed") for x in v) for v in hours.values() if v):
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


def _hours_text(hours: tuple[int, int]) -> str:
    if hours[0] == 0 and hours[1] >= 24 * 60 - 1:
        return "24 hours"
    return f"{_hhmm(hours[0])}–" + ("midnight" if hours[1] >= 24 * 60 else _hhmm(hours[1]))


def _friday(date: str | None) -> bool:
    try:
        return dt.date.fromisoformat(date).weekday() == 4
    except (TypeError, ValueError):
        return False


JUMUAH_WHY = "Jumu'ah, the Friday congregational prayer with a sermon — arrive 15 minutes early; allow about 45 minutes."


def _jumuah(final: dict, trip: Trip) -> None:
    """On Friday the noon prayer is Jumu'ah: a congregational prayer with a sermon, held in a larger mosque.
    Rihla names it and prefers a well-known mosque near the morning's stops."""
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
                st["why"] = JUMUAH_WHY
                # nothing happens during it: _untangle ends a visit before it (15 minutes early, with the way there)
                # or starts it afterwards


def _open_at(p: dict, date: str | None, t: int) -> bool:
    hours = _opening(p, date)
    return hours is None or (hours is not False and hours[0] <= t <= hours[1] - 60)


def _check_hours(final: dict, trip: Trip, drop: bool = True) -> None:
    """Visit places while they are open: a visit before opening time moves to the opening; a place closed that day
    moves to another day of the trip when it is open then, or is left out."""
    days = final.get("days", [])
    for day in days:
        for st in list(day["stops"]):
            if st.get("kind") != "sight":
                continue
            place = trip.places.get(st.get("ref"), {})
            hours = _opening(place, day.get("date"))
            t = _mins(st.get("time", "")) or 0
            if hours is False or (hours and t >= hours[1] - 45):
                if not drop:
                    continue
                day["stops"].remove(st)
                if hours is False:  # closed that day: on another day of the trip, if it is open then
                    other = min((d for d in days if d is not day and _opening(place, d.get("date")) is not False
                                 and all(x.get("ref") != st.get("ref") for x in d["stops"])),
                                key=lambda d: sum(1 for x in d["stops"] if x.get("kind") == "sight"), default=None)
                    if other is not None:
                        h = _opening(place, other.get("date"))
                        if h and not (h[0] <= t <= h[1] - 75):
                            t = (h[0] + 14) // 15 * 15
                        other["stops"].append(dict(st, time=_hhmm(t)))
                        other["stops"].sort(key=lambda x: _mins(x.get("time", "")) or 0)
                continue
            if hours:
                st["open_today"] = _hours_text(hours)
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


def _meal_score(x: dict, cc: str = "") -> float:
    """Lower is better: the surest halal first, no alcohol, a local place rather than a burger chain, their taste,
    then distance (metres). Where nearly all food is halal, "likely" is almost as good as "listed"."""
    step = 300 if cc in halal.MUSLIM_MAJORITY else 700
    return (step * RANK.get(x.get("halal_level"), 3) + 900 * bool(x.get("alcohol")) + 800 * bool(CHAINS.search(x.get("name") or ""))
            - 250 * bool(x.get("because")) - 200 * float(x.get("affinity") or 0) + 400 * bool(x.get("unnamed"))
            + float(x.get("distance_m") or 0))


LODGING_NAME = re.compile(r"\b(hotel|hostel|guest ?house|inn|residence|apartments?|ゲストハウス|ホテル)\b", re.I)


def _mosque_score(m: dict) -> float:
    """Metres, with a named mosque from Qloo (photo, description) preferred to an unnamed one on the map, and a
    real mosque to a prayer room in a hotel or guest house."""
    return (float(m.get("distance_m") or 0) + 400 * bool(m.get("unnamed")) - 250 * (m.get("source") == "qloo")
            + 600 * bool(LODGING_NAME.search(m.get("name") or "")))


def _outdoor(p: dict) -> bool:
    words = " ".join([p.get("name") or ""] + list(p.get("categories") or []))
    return bool((p.get("place_kind") == "park" or p.get("kind") == "park" or OUTDOOR.search(words)) and not INDOOR.search(words))


def _sunset(day: dict) -> int | None:
    return _mins((day.get("prayer_times") or {}).get("Maghrib", "")) or \
        next((_mins(p.get("time", "")) for p in day["stops"] if p.get("prayer") == "Maghrib"), None)


def _a_sight(x: dict, trip: Trip) -> bool:
    """Can this pool place be added as a visit? Not food, not a hotel or shop; a mosque only if it is a famous one."""
    if x.get("halal_level") or _not_a_sight(x, trip):
        return False
    cats = " ".join(x.get("categories") or [])
    if x.get("kind") == "mosque" or re.search(r"\bmosque\b", cats):
        return bool(x.get("popular") or re.search(r"tourist attraction|historical|landmark", cats))
    return True


def _fill_days(final: dict, trip: Trip) -> None:
    """A day should not have long empty stretches: fill gaps with the best nearby place the tools found
    (famous highlights and taste matches first), and make sure there is a lunch and a dinner."""
    days = final.get("days", [])
    used = {k for d in days for st in d["stops"] if st.get("kind") != "prayer" for k in [_key(st.get("name", ""))] if k}
    target = 3 if trip.kids else 4
    home = (trip.dest["lat"], trip.dest["lon"])
    cc = trip.dest.get("country_code", "")
    for day in days:
        # Dhuhr, Asr and Maghrib every day, at the mosque nearest to where the traveller is then
        have = {st.get("prayer") for st in day["stops"] if st.get("kind") == "prayer"}
        for name in ("Dhuhr", "Asr", "Maghrib"):
            t = _mins((day.get("prayer_times") or {}).get(name, ""))
            if name in have or t is None:
                continue
            mosque = min(trip.near("mosques", *_near_stop(day, t, home), 6000, 15), key=_mosque_score, default=None)
            if mosque:
                day["stops"].append(_fill_stop({"time": _hhmm(t), "kind": "prayer", "prayer": name, "ref": mosque["ref"],
                                                "added_by": "rihla", "why": ""}, mosque))
        # lunch and dinner; a meal before 11:30 (and none at lunchtime) becomes lunch at noon
        early = [st for st in day["stops"] if st.get("kind") == "meal" and 10 * 60 <= (_mins(st.get("time", "")) or 0) < 11 * 60 + 30]
        if early and not any(st.get("kind") == "meal" and 11 * 60 + 30 <= (_mins(st.get("time", "")) or 0) <= 14 * 60 + 30
                             for st in day["stops"]) and _open_at(trip.places.get(early[0].get("ref"), {}), day.get("date"), 12 * 60):
            early[0]["time"] = "12:00"
        # pray, then eat: lunch just after Dhuhr (after Jumu'ah on Fridays), never before noon
        dhuhr = _mins((day.get("prayer_times") or {}).get("Dhuhr", ""))
        lunch = max(12 * 60, dhuhr + (60 if _friday(day.get("date")) else 20)) if dhuhr and 11 * 60 <= dhuhr <= 14 * 60 else 12 * 60 + 30
        lunch = (lunch + 4) // 5 * 5
        for lo, hi, at in ((11 * 60 + 30, 14 * 60 + 30, lunch), (18 * 60, 21 * 60, None)):
            if any(st.get("kind") == "meal" and lo <= (_mins(st.get("time", "")) or 0) <= hi for st in day["stops"]):
                continue
            if at is None:  # dinner after Maghrib
                at = min(((_mins((day.get("prayer_times") or {}).get("Maghrib", "")) or 18 * 60) + 25), 20 * 60 + 30)
            here = _near_stop(day, at, home)
            brands = {k for d in days for st in d["stops"] if st.get("kind") == "meal"
                      for k in (_brand(st.get("name", "")), _key(st.get("name", "")))}
            ok = lambda x: (_key(x["name"]) not in used and not _chain_in(x["name"], brands)
                            and _open_at(x, day.get("date"), at) and not x.get("closed_for_good"))
            # where halal food is rare (Tokyo), a little farther is better than no meal
            options = [x for x in trip.near("halal", *here, 2000, 40) if ok(x)] or [x for x in trip.near("halal", *here, 3500, 40) if ok(x)]
            food = min(options, key=lambda x: _meal_score(x, cc), default=None)
            if food:
                if lo >= 18 * 60:  # dinner, not an afternoon snack that leaves no room for it
                    day["stops"] = [st for st in day["stops"] if not (st.get("kind") == "meal"
                                    and 14 * 60 + 30 < (_mins(st.get("time", "")) or 0) < at and at - (_mins(st["time"]) or 0) < MEAL_GAP)]
                day["stops"].append(_fill_stop({"time": _hhmm(at), "kind": "meal", "ref": food["ref"], "added_by": "rihla",
                                                "why": ""}, food))
                used.add(_key(food["name"]))
        # long empty stretches between 09:00 and 20:00
        sunset = _sunset(day)
        for _ in range(4):
            have_sights = sum(1 for st in day["stops"] if st.get("kind") == "sight")
            if have_sights >= target + 1:
                break
            busy = sorted(((_mins(st.get("time", "")) or 0), (_mins(st.get("time", "")) or 0) + DURATION.get(st.get("kind"), 45))
                          for st in day["stops"])
            cursor, gaps = 9 * 60, []
            for a, b in busy + [(20 * 60, 20 * 60)]:
                if a - cursor >= 95:
                    gaps.append((a - cursor, cursor))
                cursor = max(cursor, b)
            # few visits and no room for one: an afternoon snack between lunch and dinner goes first (in Tokyo the
            # prayers are close together, and three meals leave no time to see anything)
            if not gaps and have_sights < target:
                daytime = [st for st in day["stops"] if st.get("kind") == "meal" and 10 * 60 <= (_mins(st.get("time", "")) or 0) < 17 * 60 + 30]
                lunch_st = next((st for st in daytime if 11 * 60 + 30 <= (_mins(st.get("time", "")) or 0) <= 15 * 60), None)
                snack = next((st for st in daytime if st is not lunch_st), None)
                if snack is not None:
                    day["stops"].remove(snack)
                    continue
            # below the target any empty stretch is filled; at the target only a long one (2 hours or more)
            if not gaps or (have_sights >= target and max(gaps)[0] < 120):
                break
            start = (max(gaps)[1] + 10 + 14) // 15 * 15
            here = _near_stop(day, start, home)
            seen = [(st["lat"], st["lon"]) for d in days for st in d["stops"] if st.get("kind") == "sight" and st.get("lat") is not None]
            pool = [x for x in trip.near("sights", *here, 3000, 60) if _key(x["name"]) not in used and _a_sight(x, trip)
                    and _open_at(x, day.get("date"), start)
                    and not (sunset and _outdoor(x) and start + 60 > sunset)  # a park in daylight
                    # not part of a place already in the plan (the Imperial Council Hall is inside Topkapi Palace)
                    and all(osm.distance_m(x["lat"], x["lon"], a, b) > 150 for a, b in seen)]
            pick = min(pool, default=None, key=lambda x: x["distance_m"] - 900 * bool(x.get("topic")) - 700 * bool(x.get("popular"))
                       - 500 * bool(x.get("because")) - 300 * bool(x.get("kids_ok") and trip.kids)
                       + 600 * (not x.get("description")))
            if not pick:
                break
            day["stops"].append(_fill_stop({"time": _hhmm(start), "kind": "sight", "ref": pick["ref"], "why": _why(pick),
                                            "added_by": "rihla"}, pick))
            used.add(_key(pick["name"]))
        day["stops"].sort(key=lambda st: _mins(st.get("time", "")) or 0)


# on foot up to this many street km, at this many km a minute: adults 4.5 km/h; young children walk slower
# (1.8 km takes them at least 30 minutes), so their walks stop at 1.2 km. The web page uses the same numbers.
WALK = {False: (1.8, 0.075), True: (1.2, 0.06)}


def _travel_min(a: dict, b: dict, kids: bool = False) -> int:
    """Minutes from stop a to stop b: on foot up to the walking limit, farther by metro or taxi (about 15 minutes
    to get going, then 3 minutes a km). Streets are about 30% longer than the straight line."""
    if not a or not b or a.get("lat") is None or b.get("lat") is None:
        return 0
    km = osm.distance_m(a["lat"], a["lon"], b["lat"], b["lon"]) * 1.3 / 1000
    longest, pace = WALK[kids]
    return round(km / pace) if km <= longest else round(15 + 3 * km)


def _untangle(final: dict, kids: bool = False) -> int:
    """No two stops at the same time, and time to get from one to the next: each stop starts when the previous
    one is over and the walk or ride is done. Prayers stay at their exact times; a visit or meal starts before a
    prayer only if it has half an hour and the way to the mosque before it (for Jumu'ah, 15 minutes early) — then
    it ends in time ("until"); otherwise it starts after the prayer, from the mosque. A visit that would now start
    too late (after 21:30, when the place is about to close, a park after sunset) or a meal right after another
    meal is left out. Returns how many stops were left out."""
    left_out = 0
    for day in final.get("days", []):
        prayers = sorted((st for st in day["stops"] if st.get("kind") == "prayer"), key=lambda st: _mins(st.get("time", "")) or 0)
        others = sorted((st for st in day["stops"] if st.get("kind") != "prayer"), key=lambda st: _mins(st.get("time", "")) or 0)
        free, last, i, kept = 0, None, 0, []  # when and where the traveller is free again; the next prayer
        sunset = _sunset(day)
        ate, dropped = None, []  # when the last meal started; stops left out
        for st in others:
            want = _mins(st.get("time", "")) or 0
            st.pop("until", None)
            while True:
                t = max(want, free + (_travel_min(last, st, kids) if last else 0))
                p = prayers[i] if i < len(prayers) else None
                if p is None:
                    break
                start = (_mins(p.get("time", "")) or 0) - (JUMUAH_EARLY if p.get("jumuah") else 0)
                if t + MIN_STAY + _travel_min(st, p, kids) <= start:
                    break
                free, last, i = max(free, start + (45 + JUMUAH_EARLY if p.get("jumuah") else DURATION["prayer"])), p, i + 1
            closes = _mins((st.get("open_today") or "").split("–")[-1])
            must = st.get("requested")
            if t > LATEST or (st.get("kind") == "sight" and closes and t >= closes - 45) \
                    or (st.get("kind") == "sight" and not must and _outdoor(st) and sunset and t >= sunset) \
                    or (st.get("kind") == "meal" and ate is not None and (t - ate < MEAL_GAP or ate >= 17 * 60)):
                dropped.append(st.get("name") or "")
                continue  # too late, closing, dark outside, or just after another meal
            if st.get("kind") == "meal":
                ate = t
            if t != want:
                t = (t + 4) // 5 * 5
                st["time"] = _hhmm(t)
            # it ends in time for the next prayer (and the way to the mosque), and before the place closes
            end = t + DURATION.get(st.get("kind"), 45)
            if p is not None:
                end = min(end, start - _travel_min(st, p, kids))
            if st.get("kind") == "sight" and closes:
                end = min(end, closes)
            if end < t + DURATION.get(st.get("kind"), 45):
                st["until"] = _hhmm(end)
            kept.append(st)
            free, last = end, st
        left_out += len(dropped)
        day["stops"] = sorted(kept + prayers, key=lambda st: (_mins(st.get("time", "")) or 0, st.get("kind") != "prayer"))
    return left_out


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


async def _check_meals(final: dict, trip: Trip) -> int:
    """Eat near where you are, while the restaurant is open: a meal more than 1.5 km from where the traveller is
    just before it (a mosque counts) or closed at that time is swapped for the best open halal option near there
    (or waits for the opening, if that is soon). Returns how many meals had to be left out."""
    cc = trip.dest.get("country_code", "")
    removed = 0
    days = final.get("days", [])
    used = {k for d in days for st in d["stops"] if st.get("kind") != "prayer" for k in [_key(st.get("name", ""))] if k}
    used |= {_brand(st.get("name", "")) for d in days for st in d["stops"] if st.get("kind") == "meal"}
    seen_brands: set[str] = set()
    for day in days:
        for i, st in enumerate(list(day["stops"])):
            if st.get("kind") != "meal" or st.get("lat") is None:
                continue
            t = _mins(st.get("time", "")) or 0
            hours = _opening(trip.places.get(st.get("ref"), {}), day.get("date"))
            if hours and t < hours[0] <= t + 60:  # opens soon: eat a little later
                t = (hours[0] + 4) // 5 * 5
                st["time"] = _hhmm(t)
            before = [x for x in day["stops"][:day["stops"].index(st)][::-1] if x.get("lat") is not None]
            here = (before[0]["lat"], before[0]["lon"]) if before else (st["lat"], st["lon"])
            far = bool(before) and osm.distance_m(*here, st["lat"], st["lon"]) > 1500
            closed = not _open_at(trip.places.get(st.get("ref"), {}), day.get("date"), t)
            # a family meal at a place with a bar: an equally sure place without alcohol nearby is better
            drinks = bool(st.get("alcohol"))
            twice = _chain_in(st.get("name", ""), seen_brands)  # another branch of a chain already in the trip
            twice = twice or bool(CHAINS.search(st.get("name") or ""))  # a burger chain: a local place nearby is better
            seen_brands |= {_brand(st.get("name", "")), _key(st.get("name", ""))}
            if not far and not closed and not drinks and not twice:
                continue
            rank = RANK.get(st.get("halal_level"), 3)
            reach = 800 if (drinks or twice) and not far and not closed else 1500

            def options():
                return [x for x in trip.near("halal", *here, reach, 40) if _key(x["name"]) not in used and x["ref"] != st.get("ref")
                        and not _chain_in(x["name"], used | seen_brands) and RANK.get(x.get("halal_level"), 3) <= rank
                        and _open_at(x, day.get("date"), t) and not x.get("closed_for_good") and not (drinks and x.get("alcohol"))
                        and not CHAINS.search(x.get("name") or "")]
            if not options() and (far or closed):  # nothing looked up around here yet: ask Qloo and OpenStreetMap
                await _safe_tool(trip, "halal_food_near", {"lat": here[0], "lon": here[1], "radius_m": 1200})
            food = min(options(), key=lambda x: _meal_score(x, cc), default=None)
            if food:
                used.discard(_key(st.get("name", "")))
                keep = {k: st[k] for k in ("time", "kind") if k in st}
                st.clear()
                st.update(_fill_stop(dict(keep, ref=food["ref"], added_by="rihla", why=""), food))
                used.add(_key(food["name"]))
            elif closed:
                day["stops"].remove(st)  # closed then and nothing else open nearby: the day is filled again
                removed += 1
    return removed


def _space_meals(final: dict) -> None:
    """The model sometimes stacks meals (lunch at 12:30, another at 14:00, a third at 15:30): keep at most three
    a day, each at least 2.5 hours after the previous one; the free time is filled with sights afterwards."""
    for day in final.get("days", []):
        last, n = None, 0
        for st in list(day["stops"]):
            t = _mins(st.get("time", "")) if st.get("kind") == "meal" else None
            if t is None:
                continue
            if n >= 3 or (last is not None and t - last < MEAL_GAP):
                day["stops"].remove(st)
                continue
            last, n = t, n + 1


def _add_musts(final: dict, trip: Trip) -> None:
    """Places they asked for, and places known for what they said they love (the Natural History Museum for kids
    who love dinosaurs), must be in the plan — on a day when they are open, instead of a weaker stop."""
    days = final.get("days", [])
    if not days:
        return
    in_plan = {_key(st.get("name", "")) for d in days for st in d["stops"]}
    words = lambda x: {w for k in x.get("topic") or [] for w in _words(k)}
    musts = sorted((x for x in trip.places.values() if (x.get("topic") or x.get("requested")) and x.get("source") == "qloo"
                    and _a_sight(x, trip) and x.get("lat") is not None),
                   key=lambda x: (not x.get("requested"), not x.get("popular"), x.get("distance_m") or 0))
    done = set().union(*[words(x) for x in musts if _key(x["name"]) in in_plan]) if musts else set()
    done |= {w for d in days for st in d["stops"] for w in words(st)}
    added = 0
    for x in musts:
        if _key(x["name"]) in in_plan or not (x.get("requested") or (added < 3 and not words(x) <= done)):
            continue
        home = (trip.dest["lat"], trip.dest["lon"])
        open_days = [d for d in days if _opening(x, d.get("date")) is not False]
        if not open_days:
            continue
        day = min(open_days, key=lambda d: osm.distance_m(*_centre(d, home), x["lat"], x["lon"]))
        # instead of the weakest visit of that day (never one they asked for, a topic or a famous highlight)
        weak = [st for st in day["stops"] if st.get("kind") == "sight" and not st.get("topic") and not st.get("requested")
                and not st.get("popular")]
        weak.sort(key=lambda st: (bool(st.get("because")), st.get("added_by") != "rihla"))
        h = _opening(x, day.get("date"))
        t = _mins(weak[0]["time"]) if weak else 10 * 60
        if h and not (h[0] <= t <= h[1] - 75):
            t = (h[0] + 14) // 15 * 15
        new = _fill_stop({"time": _hhmm(t), "kind": "sight", "ref": x["ref"], "why": _why(x), "added_by": "rihla"}, x)
        if weak:
            day["stops"][day["stops"].index(weak[0])] = new
        else:
            day["stops"].append(new)
        day["stops"].sort(key=lambda st: _mins(st.get("time", "")) or 0)
        in_plan.add(_key(x["name"]))
        done |= words(x)
        added += 0 if x.get("requested") else 1


async def _pick_mosques(final: dict, trip: Trip) -> None:
    """Pray on the way: for each prayer the mosque that adds the least walking between the stop before and the stop
    after (the model tends to reuse one mosque all day, or one near a stop that is no longer there). Where no mosque
    is known within 1.5 km of the stop before, Rihla asks Qloo and OpenStreetMap about that spot first."""
    for d in final.get("days", []):
        for i, st in enumerate(d["stops"]):
            if st.get("kind") != "prayer" or st.get("jumuah") or trip.lookups["mosques"] <= 0:
                continue
            prev = next((x for x in d["stops"][:i][::-1] if x.get("kind") != "prayer" and x.get("lat") is not None), None)
            nxt = next((x for x in d["stops"][i + 1:] if x.get("kind") != "prayer" and x.get("lat") is not None), None)
            anchor = prev or nxt
            if anchor and not trip.near("mosques", anchor["lat"], anchor["lon"], 1500, 1):
                trip.lookups["mosques"] -= 1
                await _safe_tool(trip, "mosques_near", {"lat": anchor["lat"], "lon": anchor["lon"]})
    _choose_mosques(final, trip)


def _choose_mosques(final: dict, trip: Trip) -> None:
    for d in final.get("days", []):
        stops = d["stops"]
        for i, st in enumerate(stops):
            if st.get("kind") != "prayer" or st.get("jumuah"):
                continue
            prev = next((x for x in stops[:i][::-1] if x.get("kind") != "prayer" and x.get("lat") is not None), None)
            nxt = next((x for x in stops[i + 1:] if x.get("kind") != "prayer" and x.get("lat") is not None), None)
            anchor = prev or nxt
            if anchor is None:
                continue

            def cost(m: dict) -> float:
                way = osm.distance_m(anchor["lat"], anchor["lon"], m["lat"], m["lon"])
                if prev and nxt:
                    # half weight: where you are now matters most; the meal after can move closer to the mosque
                    way += 0.5 * osm.distance_m(m["lat"], m["lon"], nxt["lat"], nxt["lon"])
                return way + 400 * bool(m.get("unnamed")) - 250 * (m.get("source") == "qloo") \
                    + 600 * bool(LODGING_NAME.search(m.get("name") or ""))
            best = min(trip.near("mosques", anchor["lat"], anchor["lon"], 6000, 20), key=cost, default=None)
            if best is None or best["ref"] == st.get("ref"):
                continue
            if st.get("lat") is None or cost(best) < cost(st) - 300:
                keep = {k: st[k] for k in ("time", "kind", "prayer", "added_by") if k in st}
                st.clear()
                st.update(_fill_stop(dict(keep, ref=best["ref"], why=""), best))


async def _more_sights(final: dict, trip: Trip) -> None:
    """A day with time left but few places left to add around it (everything near was used, or is a hotel or a
    shop): museums, landmarks and parks from the map around that day (at most two lookups a plan)."""
    home = (trip.dest["lat"], trip.dest["lon"])
    used = {_key(st.get("name", "")) for d in final.get("days", []) for st in d["stops"]}
    seen = [(st["lat"], st["lon"]) for d in final.get("days", []) for st in d["stops"] if st.get("lat") is not None]
    for day in final.get("days", []):
        here = _centre(day, home)
        # what the day could still use: unused, a real sight, not inside a place already in the plan
        left = [x for x in trip.near("sights", *here, 3000, 80) if _key(x["name"]) not in used and _a_sight(x, trip)
                and all(osm.distance_m(x["lat"], x["lon"], a, b) > 150 for a, b in seen)]
        if len(left) < 6 and trip.lookups["sights"] > 0:
            trip.lookups["sights"] -= 1
            await _safe_tool(trip, "sights_near", {"lat": here[0], "lon": here[1], "radius_m": 3000})


async def _food_near_meals(final: dict, trip: Trip) -> None:
    """Halal food is rare in some cities (Tokyo): before lunch and dinner are added, make sure Rihla has looked for
    it around where the traveller will be at those times (at most two extra lookups a plan)."""
    home = (trip.dest["lat"], trip.dest["lon"])
    for day in final.get("days", []):
        for at in (12 * 60 + 30, (_mins((day.get("prayer_times") or {}).get("Maghrib", "")) or 18 * 60) + 25):
            here = _near_stop(day, at, home)
            if trip.lookups["halal"] > 0 and not trip.near("halal", *here, 2000, 1):
                trip.lookups["halal"] -= 1
                await _safe_tool(trip, "halal_food_near", {"lat": here[0], "lon": here[1], "radius_m": 1500})


async def _finish(final: dict, trip: Trip) -> None:
    """Everything the code checks after the model, in an order that settles: places they must see, opening hours,
    then rounds of filling empty time, routing, meals near where you are, mosques on the way and realistic times —
    until no stop has to be left out."""
    _add_musts(final, trip)
    _check_hours(final, trip)
    _space_meals(final)
    for _ in range(3):
        await _food_near_meals(final, trip)
        await _more_sights(final, trip)
        _fill_days(final, trip)
        _route(final)
        _check_hours(final, trip, drop=False)  # after the route: a place takes a slot only once it is open
        meals_left_out = await _check_meals(final, trip)
        await _pick_mosques(final, trip)
        _jumuah(final, trip)
        if not _untangle(final, trip.kids) and not meals_left_out:
            break
        _add_musts(final, trip)  # one they asked for may have been left out for its time: on another slot
        _check_hours(final, trip)
    else:  # still settling after three rounds: the last word is realistic times
        await _pick_mosques(final, trip)
        _jumuah(final, trip)
        _untangle(final, trip.kids)
    if _without_meal(final):  # a day that lost its lunch or dinner on the way gets one more round
        await _food_near_meals(final, trip)
        _fill_days(final, trip)
        _check_hours(final, trip, drop=False)
        await _check_meals(final, trip)
        await _pick_mosques(final, trip)
        _jumuah(final, trip)
        _untangle(final, trip.kids)
    _texts(final, trip)


def _without_meal(final: dict) -> bool:
    """Is there a day without lunch (11:30–15:00) or without dinner (from 17:00)?"""
    def has(day: dict, lo: int, hi: int) -> bool:
        return any(st.get("kind") == "meal" and lo <= (_mins(st.get("time", "")) or 0) <= hi for st in day["stops"])
    return any(not has(d, 11 * 60 + 30, 15 * 60) or not has(d, 17 * 60, 22 * 60) for d in final.get("days", []))


# what the model must not claim about a restaurant (the halal label in the app says it honestly)
CLAIMS = re.compile(r"halal|helal|حلال|халал|alcohol|alkol|alcool|كحول|алкохол|certif|verified|doğrulan", re.I)


def _no_claims(text: str) -> str:
    """The sentences of a meal's text that do not say whether it is halal or serves alcohol."""
    parts = re.split(r"(?<=[.!?。])\s+", (text or "").strip())
    return " ".join(p for p in parts if p and not CLAIMS.search(p)).strip()


def _cuisine_text(cuisine: str) -> str:
    """" Lebanese food." from OpenStreetMap's "lebanese;grill"; " Coffee shop." from "coffee_shop"."""
    c = re.split(r"[,;]", cuisine or "")[0].strip().replace("_", " ")
    if not c:
        return ""
    return f" {c[:1].upper() + c[1:]}." if re.search(r"shop|cafe|café|bakery|kiosk|bar|court", c, re.I) else \
        f" {c[:1].upper() + c[1:]} food."


def _texts(final: dict, trip: Trip) -> None:
    """Texts that match the final plan: every prayer says where you come from and go next; a meal the code chose
    says where it is; no text names a place that is not in that day; no meal text claims halal or no alcohol."""
    names = {k for x in trip.places.values() for k in [_key(x.get("name", ""))] if len(k) >= 8}
    for d in final.get("days", []):
        stops = d["stops"]
        here = {_key(x.get("name", "")) for x in stops}
        for i, st in enumerate(stops):
            prev = next((x for x in stops[:i][::-1] if x.get("kind") != "prayer" and x.get("name")), None)
            nxt = next((x for x in stops[i + 1:] if x.get("kind") != "prayer" and x.get("name")), None)
            why, own = _key(st.get("why", "")), _key(st.get("name", ""))
            stale = any(n != own and n not in own and n not in here and n in why for n in names)
            if st.get("kind") == "prayer":
                if st.get("jumuah"):
                    st["why"] = JUMUAH_WHY
                elif prev and nxt:
                    st["why"] = f"{st.get('prayer', 'Prayer')} on the way from {prev['name']} to {nxt['name']}."
                elif prev or nxt:
                    st["why"] = f"{st.get('prayer', 'Prayer')} at the mosque nearest to {(prev or nxt)['name']}."
            elif st.get("kind") == "meal":
                text = _no_claims(st.get("why", ""))
                t = _mins(st.get("time", "")) or 0
                # the day's first meal at lunchtime is lunch, its first in the evening dinner; any other is a break
                earlier = [_mins(x.get("time", "")) or 0 for x in stops[:i] if x.get("kind") == "meal"]
                label = ("Lunch" if 11 * 60 + 30 <= t < 15 * 60 + 30 and not any(11 * 60 + 30 <= e < 15 * 60 + 30 for e in earlier)
                         else "Dinner" if t >= 17 * 60 + 30 and not any(e >= 17 * 60 + 30 for e in earlier) else "A break")
                if st.get("added_by") == "rihla" or not text or stale:
                    near = prev and osm.distance_m(prev["lat"], prev["lon"], st["lat"], st["lon"]) <= 1200
                    food = _cuisine_text(st.get("cuisine", ""))
                    text = (f"{label} a short walk from {prev['name']}." if near else
                            f"{label} close to your stops this {'afternoon' if label != 'Dinner' else 'evening'}.") + food
                st["why"] = text
            elif stale or not st.get("why"):
                st["why"] = _why(trip.places.get(st.get("ref"), st) | {"requested": st.get("requested"), "topic": st.get("topic")})


NEARBY_FIELDS = ("name", "lat", "lon", "halal_level", "halal_reason", "alcohol", "cuisine", "because", "kids_ok", "popular",
                 "topic", "source", "categories", "rating", "unnamed")
NEARBY_MAX = {"halal": 60, "mosques": 40, "sights": 40}


def _nearby(final: dict, trip: Trip) -> dict:
    """Places the tools found that the plan does not use, for the phone on the day of the trip ("Today" mode):
    halal food, mosques and sights near wherever the traveller is — no new Qloo requests, works offline.
    One entry per chain, no hotels or shops among the sights, nothing built around alcohol."""
    used = {s.get("ref") for d in final.get("days", []) for s in d.get("stops", [])}
    used_names = {_key(s.get("name", "")) for d in final.get("days", []) for s in d.get("stops", []) if s.get("kind") != "prayer"}
    order = {
        "halal": lambda p: (bool(p.get("alcohol")), RANK.get(p.get("halal_level"), 3), bool(CHAINS.search(p.get("name") or "")),
                            -(p.get("affinity") or 0)),
        "mosques": lambda p: (bool(p.get("unnamed")), p.get("source") != "qloo"),
        "sights": lambda p: (-bool(p.get("topic")) - bool(p.get("because")) - bool(p.get("popular")), -(p.get("affinity") or 0)),
    }
    out = {}
    for kind, pool in trip.pool.items():
        items = [p for ref, p in pool.items() if ref not in used and p.get("lat") is not None and p.get("name")
                 and not p.get("closed_for_good") and not qloo.is_alcohol_venue(p) and len(_key(p["name"])) >= 2
                 and (kind == "mosques" or _key(p["name"]) not in used_names)
                 and (kind != "sights" or (_a_sight(p, trip) and not p.get("alcohol")))
                 and (kind != "halal" or not _not_food(p))]
        items.sort(key=order[kind])
        rows, seen = [], set()
        for p in items:
            k = _key(p["name"])
            if kind != "mosques" and k in seen:
                continue  # one branch of a chain is enough
            seen.add(k)
            row = {k: p[k] for k in NEARBY_FIELDS if p.get(k) not in (None, "", [], False)}
            row["lat"], row["lon"] = round(p["lat"], 5), round(p["lon"], 5)
            if row.get("categories"):
                row["categories"] = row["categories"][:1]
            rows.append(row)
            if len(rows) >= NEARBY_MAX[kind]:
                break
        out[kind] = rows
    return out


def _brand(name: str) -> str:
    """A restaurant chain without the branch: "Pizza Pilgrims London Bridge" -> "pizza pilgrims"."""
    words = _key(name).split()
    return " ".join(words[:2]) if len(words) >= 3 else " ".join(words)


def _chain_in(name: str, brands: set[str]) -> bool:
    """Is this restaurant a branch of one already in the trip? Same brand, or one name starts with the other
    ("Hiba" / "Hiba Express")."""
    k, b = _key(name), _brand(name)
    first = k.split()[0] if k else ""
    own = len(first) >= 4 and first not in FOOD_WORDS  # "Hiba Express" and "Hiba Street", not "Chicken Shop" and "Chicken Street"
    return bool(k) and any(x and (x == b or k.startswith(x + " ") or x.startswith(k + " ") or x == k
                                  or (own and x.split()[0] == first)) for x in brands)


FOOD_WORDS = set("""pizza burger burgers kebab kebap halal chicken grill house star royal golden sushi curry tandoori
shawarma falafel noodle noodles taco tacos bistro kitchen cafe coffee tea bakery sweet sweets dessert ramen fried
spice spicy steak steakhouse diner food foods express fresh green little big best city london paris istanbul new""".split())


def _twin(a: dict, b: dict) -> bool:
    """Two branches of one institution in a trip ("Salt Galata" and "Salt Beyoğlu"): same first word, two-word
    names, same kind of place."""
    ka, kb = _key(a.get("name", "")).split(), _key(b.get("name", "")).split()
    return len(ka) == len(kb) == 2 and ka[0] == kb[0] and len(ka[0]) >= 4 and \
        (a.get("categories") or [""])[:1] == (b.get("categories") or [""])[:1]


async def _enrich(final: dict, trip: Trip, prayers: list[dict]) -> dict:
    """Attach real coordinates and details to every stop; drop stops that reference unknown places."""
    last = (trip.dest["lat"], trip.dest["lon"])
    used: set[str] = set()
    used_names: set[str] = set()
    used_points: list[tuple[float, float]] = []  # the same museum can exist twice in the data under two names
    sights: list[dict] = []
    for i, day in enumerate(final.get("days", [])):
        day["day"] = i + 1  # whatever the model numbered it
        for s in day.get("stops", []):
            t = _mins(s.get("time", ""))
            s["time"] = _hhmm(t) if t is not None else "12:00"
        day["date"] = prayers[i]["date"] if i < len(prayers) else None
        day["hijri"] = prayers[i]["hijri"] if i < len(prayers) else None
        day["prayer_times"] = prayers[i]["timings"] if i < len(prayers) else {}
        stops, prayed = [], set()
        for s in day.get("stops", []):
            p = trip.places.get(s.get("ref", ""))
            want = s.get("kind")
            is_food = bool(p and p.get("halal_level"))
            is_mosque = bool(p and p.get("kind") == "mosque" or p and re.search(r"\bmosque\b", " ".join(p.get("categories") or [])))
            # the model sometimes files a sight under "meal" or a cafe under "prayer": fix it from the data
            if want == "prayer" and not is_mosque:
                p = min(trip.near("mosques", *last, 6000, 15), key=_mosque_score, default=None)
                s["why"] = ""
            elif want == "meal" and not is_food:
                options = [x for x in trip.near("halal", *last, 2500, 30) if x["ref"] not in used and _key(x["name"]) not in used_names]
                p = min(options, key=lambda x: _meal_score(x, trip.dest.get("country_code", "")), default=None)
                s["why"], s["added_by"] = "", "rihla"
            elif want == "sight" and is_food:
                s["kind"] = "meal"
            elif want == "sight" and is_mosque and not _a_sight(p, trip):
                s["kind"] = "prayer"  # a famous mosque (the Blue Mosque) stays a visit; a small one is for praying
            elif want == "sight" and p is not None and not is_mosque and _not_a_sight(p, trip):
                continue  # a hotel or a shop the model picked as a sight
            if p:
                s["ref"] = p.get("ref", s.get("ref"))
            if s.get("kind") == "prayer" and day.get("prayer_times"):
                # a prayer stop sits exactly at the prayer time, whatever time the model wrote
                name = _prayer_name(s.get("time", ""), day["prayer_times"])
                if name in prayed:
                    continue  # one stop per prayer (the model sometimes lists two mosques for Dhuhr)
                prayed.add(name)
                s["time"], s["prayer"] = day["prayer_times"][name], name
            if not p:
                continue
            if s.get("kind") not in ("sight", "meal", "prayer", "rest"):
                s["kind"] = "prayer" if p.get("kind") == "mosque" else "meal" if p.get("halal_level") else "sight"
            twin = s["kind"] == "sight" and p.get("lat") is not None and \
                (any(osm.distance_m(p["lat"], p["lon"], a, b) < 80 for a, b in used_points) or any(_twin(p, x) for x in sights))
            if s["kind"] != "prayer" and (s.get("ref") in used or _key(p.get("name", "")) in used_names or twin):
                # never the same sight or restaurant twice in one trip (not even another branch of a chain)
                if s["kind"] != "meal":
                    continue
                options = [x for x in trip.near("halal", *last, 2500, 30) if x["ref"] not in used and _key(x["name"]) not in used_names]
                p = min(options, key=lambda x: _meal_score(x, trip.dest.get("country_code", "")), default=None)
                if not p:
                    continue
                s["ref"], s["why"], s["added_by"] = p["ref"], "", "rihla"
            used.add(s.get("ref"))
            if s["kind"] != "prayer":
                used_names.add(_key(p.get("name", "")))
            if s["kind"] == "sight" and p.get("lat") is not None:
                used_points.append((p["lat"], p["lon"]))
                sights.append(p)
            _fill_stop(s, p)
            if p.get("lat"):
                last = (p["lat"], p["lon"])
            stops.append(s)
        day["stops"] = sorted(stops, key=lambda x: _mins(x.get("time", "")) or 0)
    await _finish(final, trip)
    final["nearby"] = _nearby(final, trip)
    final["destination"] = trip.dest
    final["timezone"] = next((p["timezone"] for p in prayers if p.get("timezone")), "")  # the plan's times are local there
    if any(p.get("missing") for p in prayers):
        final.setdefault("tips", []).insert(0, "Prayer times could not be loaded just now: please check the times at a local mosque.")
    final["trace"] = trip.trace
    final["signals"] = list(trip.signals.values())
    final["topics"] = list(dict.fromkeys((trip.tags or {}).values()))  # their topics Qloo knows as tags
    final["audiences"] = ["Islam"] + (["Parents with young children"] if trip.kids else [])
    final["qloo_mode"] = "mock" if config.QLOO_MOCK else "live"
    final["model"] = trip.model
    final["llm_log"] = getattr(trip, "llm_log", [])
    return final


EDIT = """You are the editor of a finished family travel plan. The stops are final: code has checked every place, time,
prayer and meal. Write in {language}. Answer with ONE JSON object and nothing else:
{"title": "...", "summary": "...", "themes": ["one per day"], "tips": ["..."], "why": {"<stop id>": "..."}}
- title: short, fits these stops; call the city what the traveller called it ("city"). summary: two sentences — who the trip is for and what it covers; name at most two or
  three highlights (never list every stop, mosque or restaurant) and only places that are in the stops.
- themes: one short theme per day that fits that day's stops (not places from other days).
- tips: up to 4 practical tips, taken from the draft tips; leave out any tip that names a place not in the stops.
- why: {why_rule}
Rules: use only the facts given; never invent exhibits, links or reasons; never say whether a restaurant is halal,
certified or serves alcohol (the app shows an honest label); do not mention fans or taste data."""


async def _edit_texts(plan: dict, trip: Trip, llm: "LLM", travellers: str, tastes: str) -> None:
    """One short call to the model after the code checks: the title, summary, day themes and tips are rewritten to
    match the final stops (the model wrote them before code moved, added or left out places), and in a plan in
    another language the texts the code wrote are translated. If the model is slow or down, the plan stays as it is."""
    if llm.left() < 30 or not plan.get("days"):
        return
    english = trip.language.lower().startswith("english")
    sights_only = english  # in English only the visits' texts are checked; in another language all are translated
    days, ids = [], {}
    for d in plan["days"]:
        rows = []
        for j, st in enumerate(d["stops"]):
            sid = f"{d['day']}-{j + 1}"
            ids[sid] = st
            row = {"id": sid, "time": st.get("time"), "kind": st.get("kind"), "name": st.get("name")}
            for k, v in (("prayer", st.get("prayer")), ("jumuah", st.get("jumuah")), ("categories", (st.get("categories") or [])[:2]),
                         ("about", _short(st.get("description", ""), 160)), ("known_for", st.get("topic")),
                         ("you_asked_for_it", st.get("requested")), ("cuisine", st.get("cuisine"))):
                if v:
                    row[k] = v
            if not english or st.get("kind") == "sight":
                row["draft"] = st.get("why", "")
            rows.append(row)
        days.append({"day": d["day"], "date": d.get("date"), "draft_theme": d.get("theme", ""), "stops": rows})
    why_rule = ("for every stop of kind \"sight\", one short sentence: keep the draft if every claim in it is in the facts "
                "given (about, known_for, categories); otherwise rewrite it from those facts. A link to their tastes only "
                "if known_for says so" if sights_only else
                "for EVERY stop id, one short sentence in {language}: translate the draft if it is right, otherwise write "
                "it from the facts given")
    prompt = EDIT.replace("{why_rule}", why_rule).replace("{language}", trip.language)
    data = {"city": trip.typed, "travellers": travellers, "tastes": tastes, "draft_title": plan.get("title", ""),
            "draft_summary": plan.get("summary", ""), "draft_tips": plan.get("tips", []), "days": days}
    try:
        r = await llm.chat(messages=[{"role": "system", "content": prompt},
                                     {"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
                           temperature=0.2, max_tokens=3500 if not english else 2000, response_format={"type": "json_object"})
    except Exception:
        return
    out = _json(r.choices[0].message.content or "")
    if not isinstance(out, dict):
        return
    text = lambda v, n: v.strip() if isinstance(v, str) and 3 <= len(v.strip()) <= n else None
    if text(out.get("title"), 120):
        plan["title"] = text(out.get("title"), 120)
    if text(out.get("summary"), 600):
        plan["summary"] = text(out.get("summary"), 600)
    themes = out.get("themes")
    if isinstance(themes, list) and len(themes) == len(plan["days"]):
        for d, th in zip(plan["days"], themes):
            if text(th, 90):
                d["theme"] = text(th, 90)
    tips = out.get("tips")
    if isinstance(tips, list):
        plan["tips"] = [t.strip() for t in tips if text(t, 300)][:4] or plan.get("tips", [])
    whys = out.get("why")
    if isinstance(whys, dict):
        for sid, w in whys.items():
            st = ids.get(str(sid))
            if st is not None and sights_only and st.get("kind") != "sight":
                continue
            w = text(w, 300)
            if st is not None and w:
                st["why"] = _no_claims(w) if st.get("kind") == "meal" else w
                if not st["why"]:
                    st["why"] = w if not CLAIMS.search(w) else st.get("why", "")
    trip.trace.append({"tool": "edit_texts", "args": {}, "found": len(plan["days"]),
                       "note": "title, day themes and texts matched to the final stops"})


async def _wiki_photos(plan: dict) -> None:
    """A photo for visits that have none (Qloo's photo links for some places no longer open): the Wikipedia
    article about the place at the same spot, if there is one."""
    todo = [st for d in plan.get("days", []) for st in d["stops"] if st.get("kind") == "sight" and not st.get("image")
            and st.get("lat") is not None and st.get("name")]
    if not todo:
        return
    try:
        found = await asyncio.wait_for(asyncio.gather(*[wiki.photo(st["name"], st["lat"], st["lon"]) for st in todo],
                                                      return_exceptions=True), 8)
    except Exception:
        return
    for st, url in zip(todo, found):
        if isinstance(url, str) and url:
            st["image"] = url
