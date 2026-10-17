"""Offline tests for what a sweep of 14 real plans and a judge-style review found (11.10.2026): places closed all week,
asked-for places lost, hotels and shops as sights, honest "because you love", mosques on the way, visits that end in time
for the prayer, texts that match the final plan, nameless mosques, non-Latin names, alcohol venues, broken photos.
Run: pytest -q"""
import asyncio

import pytest

from app import agent, halal, osm, prayer, qloo

LONDON = {"lat": 51.5074, "lon": -0.1278, "country_code": "gb", "name": "London, Greater London, England"}


def trip(tastes="", travellers="Couple", typed="London", dest=LONDON):
    t = agent.Trip(dest, travellers, tastes, typed)
    t.tags = {}
    return t


def sight(name, lat, lon, **kw):
    return dict({"name": name, "lat": lat, "lon": lon, "kind": "landmark", "categories": ["museum"]}, **kw)


# ---------- opening hours ----------
WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def test_closed_every_day_means_unknown_hours():
    gallery = {"hours": {d: [{"opens": None, "closes": None, "closed": True}] for d in WEEK}}
    assert agent._opening(gallery, "2026-10-16") is None and agent._open_at(gallery, "2026-10-16", 600)


def test_a_place_closed_that_day_moves_to_another_day():
    t = trip()
    ski = sight("Ski Dubai", 51.5, -0.12, hours={"friday": [{"closed": True}],
                                                 "monday": [{"opens": "T10:00:00", "closes": "T23:00:00", "closed": False}]})
    t.keep("q:ski", ski, "qloo", "sights")
    final = {"days": [{"date": "2026-10-16", "stops": [{"time": "11:00", "kind": "sight", "ref": "q:ski", "name": "Ski Dubai"}]},
                      {"date": "2026-10-19", "stops": []}]}
    agent._check_hours(final, t)
    assert final["days"][0]["stops"] == [] and [s["name"] for s in final["days"][1]["stops"]] == ["Ski Dubai"]


# ---------- what the traveller asked for ----------
def test_asked_for_place_keeps_its_flag_when_another_tool_returns_it():
    t = trip()
    t.keep("q:alh", sight("Alhambra", 37.176, -3.588, requested=True), "qloo", "sights")
    t.keep("q:alh", sight("Alhambra", 37.176, -3.588), "qloo", "sights")  # later from taste_places, without the flag
    assert t.places["q:alh"]["requested"] and t.pool["sights"]["q:alh"]["requested"]


def test_names_the_model_did_not_look_up_are_looked_up(monkeypatch):
    t = trip("The kids love Frozen. We want to see the Burj Khalifa.")
    t.trace.append({"tool": "taste_entities", "args": {"items": [{"query": "Frozen"}]}, "found": 1, "note": ""})
    asked = []

    async def run(trip_, name, args):
        asked.append([x["query"] for x in args["items"]])
        return [], "ok"
    monkeypatch.setattr(agent, "_run_tool", run)
    asyncio.run(agent._missed_names(t, t.tastes, agent._noop))
    assert asked == [["Burj Khalifa"]]


def test_a_topic_is_not_a_podcast(monkeypatch):
    async def search(q, kind=None, take=3):
        return [{"id": "p1", "name": "Ottoman History Podcast", "type": "urn:entity:podcast", "popularity": 0.9}]
    monkeypatch.setattr(qloo, "search", search)
    t = trip("We love Ottoman history and calligraphy")
    found, _ = asyncio.run(agent._lookup_one(t, "Ottoman history", "Ottoman history", None))
    assert found == [] and not t.signals


# ---------- not a sight ----------
@pytest.mark.parametrize("place, out", [
    ({"name": "The Sherry-Netherland", "categories": ["historical landmark", "hotel"]}, True),
    ({"name": "Seri Negara", "categories": ["historical landmark"], "description": "Genteel suites with butler service"}, True),
    ({"name": "RYOKAN KAMOGAWA", "categories": ["tourist attraction", "japanese inn"]}, True),
    ({"name": "Mirador de Morayma", "categories": ["andalusian restaurant", "garden", "tourist attraction"]}, True),
    ({"name": "5 STAR MOVERS", "categories": ["park", "storage facility", "movers"]}, True),
    ({"name": "Hertha BSC", "categories": ["sports memorabilia store"]}, True),
    ({"name": "Somewhere", "categories": ["museum"], "lodging": True}, True),
    ({"name": "Sofia, Bulgaria", "categories": []}, True),                                   # the city itself
    ({"name": "Platform 9 3/4", "categories": ["gift shop", "tourist attraction"]}, False),   # a shop that is a sight
    ({"name": "Grand Bazaar", "categories": ["market", "tourist attraction"]}, False),
    ({"name": "Natural History Museum", "categories": ["museum"]}, False),
])
def test_not_a_sight(place, out):
    t = trip(typed="Sofia, Bulgaria", dest={"lat": 42.7, "lon": 23.3, "country_code": "bg", "name": "Sofia, Sofia City, Bulgaria"})
    assert agent._not_a_sight(place, t) == out


def test_a_bookstore_is_a_sight_when_they_asked_for_bookstores():
    store = {"name": "Robinson Crusoe 389", "categories": ["book store"]}
    assert agent._not_a_sight(store, trip()) and not agent._not_a_sight(store, trip(), {"book store"})


def test_childrens_museum_is_for_children_and_macabre_is_not():
    kids = {"name": "Science Center Spectrum", "categories": ["children s museum"]}
    grim = {"name": "Hunterian Museum", "categories": ["museum"], "description": "Macabre collection of anatomical specimens"}
    adults, family = trip(travellers="Three friends in their 20s"), trip(travellers="Family, kids aged 6 and 9")
    assert agent._not_a_sight(kids, adults) and not agent._not_a_sight(kids, family)
    assert agent._not_a_sight(grim, family) and not agent._not_a_sight(grim, adults)


# ---------- honest "because you love X" ----------
def _place(i, name):
    return {"entity_id": i, "name": name, "location": {"lat": 42.69, "lon": 23.32}, "tags": [{"id": "urn:tag:category:place:museum"}]}


def test_no_badge_where_the_taste_does_not_lift_the_place(monkeypatch):
    async def get(path, params):  # a small city: the taste's top 10 is the city's famous ten
        return {"results": {"entities": [_place(f"p{i}", f"Place {i}") for i in range(12)]}}
    monkeypatch.setattr(qloo, "_get", get)
    t = trip("Frozen", typed="Sofia", dest={"lat": 42.7, "lon": 23.3, "country_code": "bg", "name": "Sofia"})
    t.signals = {"frozen": "Frozen"}
    out = asyncio.run(agent._taste_places(t, ["museum"], {}))["museum"]
    assert out and not any(p.get("taste_match") for p in out)


def test_badge_where_the_taste_lifts_the_place(monkeypatch):
    async def get(path, params):
        if "signal.interests.entities" in params:
            return {"results": {"entities": [_place("hp", "Leadenhall Market")] + [_place(f"p{i}", f"Place {i}") for i in range(9)]}}
        return {"results": {"entities": [_place(f"p{i}", f"Place {i}") for i in range(12)]}}
    monkeypatch.setattr(qloo, "_get", get)
    t = trip("Harry Potter")
    t.signals = {"hp": "Harry Potter"}
    out = {p["name"]: p for p in asyncio.run(agent._taste_places(t, ["museum"], {}))["museum"]}
    assert out["Leadenhall Market"].get("taste_match") and not out["Place 0"].get("taste_match")


@pytest.mark.parametrize("keywords, hits", [
    (["dinosaurs"], ["dinosaurs"]), (["street art"], ["street art"]), (["street food"], []), (["ottoman house"], []),
])
def test_known_for_only_what_they_wrote(keywords, hits):
    t = trip("The kids love dinosaurs and street art")
    assert agent._topic_hits(t, {"known_for": keywords, "tag_ids": []}) == hits


def test_known_for_keeps_the_obvious_extra_word():
    t = trip("He loves dinosaurs; we love street food")
    assert agent._topic_hits(t, {"known_for": ["dinosaur fossils", "street art"], "tag_ids": []}) == ["dinosaur fossils"]


def test_the_teams_name_is_not_a_topic():
    t = trip("We love the Toronto Maple Leafs and hockey", typed="Toronto",
             dest={"lat": 43.65, "lon": -79.38, "country_code": "ca", "name": "Toronto, Ontario, Canada"})
    t.named |= agent._words("Toronto Maple Leafs")
    t.topics -= agent._words("Toronto Maple Leafs")
    assert t.topics == {"hockey"}
    assert agent._topic_hits(t, {"known_for": ["toronto railway museum"]}) == []


# ---------- the day: mosques on the way, visits that end in time ----------
def test_prayer_at_the_mosque_on_the_way():
    t = trip()
    t.keep("m:far", {"name": "Far Mosque", "lat": 51.53, "lon": -0.12, "kind": "mosque"}, "qloo", "mosques")
    t.keep("m:way", {"name": "Way Mosque", "lat": 51.5005, "lon": -0.115, "kind": "mosque"}, "qloo", "mosques")
    day = {"stops": [{"time": "11:00", "kind": "sight", "name": "A", "lat": 51.5, "lon": -0.12},
                     {"time": "12:46", "kind": "prayer", "prayer": "Dhuhr", "ref": "m:far", "name": "Far Mosque", "lat": 51.53, "lon": -0.12},
                     {"time": "14:00", "kind": "sight", "name": "B", "lat": 51.501, "lon": -0.11}]}
    asyncio.run(agent._pick_mosques({"days": [day]}, t))
    assert day["stops"][1]["name"] == "Way Mosque" and day["stops"][1]["time"] == "12:46"


def test_a_visit_ends_in_time_to_walk_to_the_mosque():
    final = {"days": [{"stops": [
        {"time": "11:30", "kind": "sight", "name": "Museum", "lat": 51.50, "lon": -0.12},
        {"time": "12:46", "kind": "prayer", "name": "Mosque", "lat": 51.505, "lon": -0.12},   # ~10 min on foot
    ]}]}
    agent._untangle(final)
    museum = final["days"][0]["stops"][0]
    assert museum["time"] == "11:30" and museum["until"] == "12:36"


def test_no_meal_just_before_jumuah_across_town():
    final = {"days": [{"stops": [
        {"time": "12:40", "kind": "meal", "name": "Lunch", "lat": 48.87, "lon": 2.34},
        {"time": "13:35", "kind": "prayer", "name": "Grande Mosquée", "jumuah": True, "lat": 48.842, "lon": 2.355},
    ]}]}
    agent._untangle(final)
    lunch = next(s for s in final["days"][0]["stops"] if s["kind"] == "meal")
    assert agent._mins(lunch["time"]) >= agent._mins("14:35")   # after the Friday prayer, from the mosque


def test_texts_match_the_final_day():
    t = trip()
    t.keep("x", {"name": "CERU Farringdon", "lat": 51.52, "lon": -0.10}, "qloo", "halal")
    stops = [{"time": "11:00", "kind": "sight", "name": "Natural History Museum", "lat": 51.4967, "lon": -0.1764, "why": "Dinosaurs."},
             {"time": "12:46", "kind": "prayer", "prayer": "Dhuhr", "name": "Mosque", "lat": 51.5, "lon": -0.17,
              "why": "Dhuhr at the mosque nearest to CERU Farringdon."},
             {"time": "13:30", "kind": "meal", "name": "Diner", "lat": 51.497, "lon": -0.17,
              "why": "A cozy lunch without alcohol. Great burgers for the kids."}]
    agent._texts({"days": [{"stops": stops}]}, t)
    assert "Natural History Museum" in stops[1]["why"] and "CERU" not in stops[1]["why"]
    assert stops[2]["why"] == "Great burgers for the kids."


def test_meal_text_of_a_swapped_meal_says_where_it_is():
    t = trip()
    stops = [{"time": "11:00", "kind": "sight", "name": "British Museum", "lat": 51.5194, "lon": -0.127},
             {"time": "13:00", "kind": "meal", "name": "Diner", "lat": 51.52, "lon": -0.126, "added_by": "rihla", "why": "",
              "cuisine": "lebanese"}]
    agent._texts({"days": [{"stops": stops}]}, t)
    assert stops[1]["why"] == "Lunch a short walk from British Museum. Lebanese food."


# ---------- food ----------
def test_a_local_place_before_a_burger_chain_where_food_is_halal_anyway():
    mcd = {"name": "McDonald's", "halal_level": "verified", "distance_m": 100}
    local = {"name": "Kasap Osman", "halal_level": "likely", "distance_m": 400}
    assert agent._meal_score(local, "tr") < agent._meal_score(mcd, "tr")
    # elsewhere a sure halal chain still beats a "likely" place, but not a sure local one
    sure = {"name": "Beirut Cafe", "halal_level": "verified", "distance_m": 600}
    assert agent._meal_score(mcd, "gb") < agent._meal_score(local, "gb") and agent._meal_score(sure, "gb") < agent._meal_score(mcd, "gb")


def test_closed_restaurant_is_swapped_for_an_open_one():
    t = trip()
    closed = {"name": "Lunch Only", "lat": 51.5, "lon": -0.12, "halal_level": "verified",
              "hours": {"saturday": [{"opens": "T11:00:00", "closes": "T14:00:00", "closed": False}],
                        "friday": [{"opens": "T11:00:00", "closes": "T14:00:00", "closed": False}]}}
    t.keep("q:closed", closed, "qloo", "halal")
    t.keep("q:open", {"name": "Open Late", "lat": 51.501, "lon": -0.12, "halal_level": "verified"}, "qloo", "halal")
    day = {"date": "2026-10-17", "stops": [{"time": "16:00", "kind": "sight", "name": "A", "lat": 51.5, "lon": -0.121},
                                          dict(closed, time="18:30", kind="meal", ref="q:closed")]}
    asyncio.run(agent._check_meals({"days": [day]}, t))
    assert day["stops"][1]["name"] == "Open Late"


@pytest.mark.parametrize("tags, name, out", [
    (["urn:tag:category:place:live_music_bar"], "Steak and Company", True),
    (["urn:tag:decor:qloo:american_beer_hall"], "Wagyu to Worldwide", True),
    (["urn:tag:category:place:cabaret_club"], "Moulin Rouge", True),
    (["urn:tag:category:place:sushi_bar"], "Sushi Place", False),
    ([], "Hofbräu Brauhaus", True),
])
def test_alcohol_centred_venues(tags, name, out):
    assert qloo.is_alcohol_venue({"name": name, "tag_ids": tags}) == out


def test_nearby_has_one_branch_per_chain_and_no_junk():
    t = trip()
    for i in range(4):
        t.keep(f"q:eln{i}", {"name": "EL&N", "lat": 51.5 + i / 100, "lon": -0.12, "halal_level": "likely"}, "qloo", "halal")
    t.keep("q:hotel", {"name": "The Clock Towers", "lat": 51.51, "lon": -0.12, "categories": ["hotel"]}, "qloo", "sights")
    t.keep("q:mus", {"name": "Postal Museum", "lat": 51.52, "lon": -0.11, "categories": ["museum"]}, "qloo", "sights")
    near = agent._nearby({"days": []}, t)
    assert [p["name"] for p in near["halal"]] == ["EL&N"] and [p["name"] for p in near["sights"]] == ["Postal Museum"]


# ---------- data from the map and from Qloo ----------
def test_geocoding_picks_the_city_people_mean():
    dubai = [{"importance": 0.7025, "category": "boundary", "type": "administrative", "addresstype": "city", "lat": "25.074"},
             {"importance": 0.7025, "category": "place", "type": "city", "addresstype": "city", "lat": "25.265"}]
    mecca = [{"importance": 0.49, "category": "place", "type": "hamlet", "addresstype": "hamlet", "lat": "45.3"},
             {"importance": 0.69, "category": "boundary", "type": "administrative", "addresstype": "city", "lat": "21.42"}]
    assert osm.best_match(dubai)["lat"] == "25.265" and osm.best_match(mecca)["lat"] == "21.42"


def test_a_mosque_without_a_name_on_the_map_still_has_one():
    el = {"type": "node", "id": 1, "lat": 25.2, "lon": 55.3, "tags": {"amenity": "place_of_worship", "religion": "muslim"}}
    p = osm._place(el, 25.2, 55.3)
    assert p["name"] == "Mosque" and p["unnamed"]
    el["tags"]["name:ar"] = "مسجد الفاروق"
    assert osm._place(el, 25.2, 55.3)["name"] == "مسجد الفاروق"


def test_names_in_any_script_can_be_compared():
    assert halal._norm("Халялко") and agent._key("Халялко") != agent._key("Паметник на Съветската армия")
    assert halal._norm("Sultan Köşesi") == "sultan kosesi"


def test_broken_google_photo_links_are_skipped():
    e = {"entity_id": "x", "name": "Neva", "properties": {"images": [
        {"url": "https://lh3.googleusercontent.com/gps-cs-s/AHRPTW"}], "check_in_time": "15:00"}}
    item = qloo._simplify(e)
    assert item["image"] is None and item["lodging"]


def test_opening_text_and_hijri_month():
    assert agent._hours_text((6 * 60, 24 * 60)) == "06:00–midnight" and agent._hours_text((0, 24 * 60)) == "24 hours"
    assert prayer.month_name("Jumādá al-ūlá") == "Jumada al-Ula"


def test_descriptions_are_cut_at_a_sentence():
    text = "A museum of the post. It has a ride on the mail rail under the streets of London, loved by children of all ages."
    assert agent._short(text, 60) == "A museum of the post."


def test_a_halal_restaurant_with_a_bar_is_only_likely():
    assert halal.level({"halal": "only", "alcohol": True})[0] == "likely"
    assert halal.level({"qloo_halal": "listed", "alcohol": True})[0] == "likely"


def test_one_branch_of_a_chain_per_trip_and_no_bar_when_there_is_a_choice():
    t = trip()
    t.keep("q:pp2", {"name": "Pizza Pilgrims London Bridge", "lat": 51.505, "lon": -0.09, "halal_level": "likely"}, "qloo", "halal")
    t.keep("q:ok", {"name": "Bao Halal", "lat": 51.5052, "lon": -0.0905, "halal_level": "likely"}, "qloo", "halal")
    t.keep("q:bar", {"name": "Steak Bar Grill", "lat": 51.52, "lon": -0.15, "halal_level": "likely", "alcohol": True}, "qloo", "halal")
    t.keep("q:dry", {"name": "Dry Diner", "lat": 51.5202, "lon": -0.1502, "halal_level": "likely"}, "qloo", "halal")
    d1 = {"date": "2026-10-17", "stops": [{"time": "12:00", "kind": "sight", "name": "Museum", "lat": 51.52, "lon": -0.1501},
                                         {"time": "13:00", "kind": "meal", "ref": "q:bar", "name": "Steak Bar Grill", "lat": 51.52,
                                          "lon": -0.15, "halal_level": "likely", "alcohol": True},
                                         {"time": "19:00", "kind": "meal", "ref": "q:pp1", "name": "Pizza Pilgrims Selfridges",
                                          "lat": 51.514, "lon": -0.152, "halal_level": "likely"}]}
    d2 = {"date": "2026-10-18", "stops": [{"time": "18:00", "kind": "sight", "name": "Bridge", "lat": 51.5051, "lon": -0.0901},
                                         {"time": "19:00", "kind": "meal", "ref": "q:pp2", "name": "Pizza Pilgrims London Bridge",
                                          "lat": 51.505, "lon": -0.09, "halal_level": "likely"}]}
    asyncio.run(agent._check_meals({"days": [d1, d2]}, t))
    assert d1["stops"][1]["name"] == "Dry Diner" and d2["stops"][1]["name"] == "Bao Halal"


def test_dinner_rather_than_an_afternoon_snack():
    t = trip()
    t.keep("q:din", {"name": "Dinner Place", "lat": 51.5, "lon": -0.12, "halal_level": "verified"}, "qloo", "halal")
    t.keep("q:lun", {"name": "Lunch Place", "lat": 51.5, "lon": -0.121, "halal_level": "verified"}, "qloo", "halal")
    day = {"date": "2026-10-17", "prayer_times": {"Maghrib": "17:45"}, "stops": [
        {"time": "16:15", "kind": "meal", "name": "Snack Cafe", "lat": 51.5, "lon": -0.12},
        {"time": "16:50", "kind": "sight", "name": "Museum", "lat": 51.5, "lon": -0.12}]}
    agent._fill_days({"days": [day]}, t)
    meals = [s["name"] for s in day["stops"] if s["kind"] == "meal"]
    assert "Snack Cafe" not in meals and "Dinner Place" in meals


@pytest.mark.parametrize("name, cats", [("Toronto bicycle tour & events", []), ("The Centre Pompidou", ["art museum"]),
                                        ("Two Temple Place", ["event venue"])])
def test_more_places_that_are_not_visits(name, cats):
    assert agent._not_a_sight({"name": name, "categories": cats}, trip())


@pytest.mark.parametrize("name, brands, same", [
    ("Hiba Express", {"hiba"}, True), ("Anam Authentic Indian Cuisine Ginza Branch", {"anam"}, True),
    ("Pizza Pilgrims Selfridges", {"pizza pilgrims"}, True), ("Star Burger", {"star kebab"}, False),
])
def test_branches_of_one_chain(name, brands, same):
    assert agent._chain_in(name, brands) == same


def test_a_prayer_room_in_a_guest_house_comes_after_a_mosque():
    room = {"name": "Guest House En ASAKUSA", "distance_m": 300}
    mosque = {"name": "As-Salaam Masjid", "distance_m": 700}
    assert agent._mosque_score(mosque) < agent._mosque_score(room)


def test_known_for_the_team_they_love():
    t = trip("We love the New York Knicks", typed="Manhattan, New York",
             dest={"lat": 40.75, "lon": -73.99, "country_code": "us", "name": "Manhattan, New York County"})
    t.named |= agent._words("New York Knicks")
    assert agent._topic_hits(t, {"known_for": ["new york knicks", "concerts"]}) == ["New York Knicks"]


@pytest.mark.parametrize("name, brands, same", [("Hiba Street", {"hiba express"}, True), ("Chicken Street", {"chicken shop"}, False)])
def test_chains_by_their_own_first_word(name, brands, same):
    assert agent._chain_in(name, brands) == same


def test_one_topic_once():
    t = trip("The kids love dinosaurs")
    assert agent._topic_hits(t, {"known_for": ["dinosaur skeletons", "dinosaurs"], "tag_ids": []}) == ["dinosaurs"]


def test_one_dinner_however_late_the_second():
    final = {"days": [{"stops": [{"time": "18:45", "kind": "meal", "name": "Dinner"},
                                 {"time": "21:20", "kind": "meal", "name": "Second dinner"}]}]}
    agent._untangle(final)
    assert [s["name"] for s in final["days"][0]["stops"]] == ["Dinner"]


@pytest.mark.parametrize("place, out", [({"name": "Sainsbury's", "categories": ["grocery store"]}, True),
                                        ({"name": "Topgolf Dubai", "categories": ["golf driving range"]}, True),
                                        ({"name": "Hiba Express", "categories": ["lebanese restaurant"]}, False)])
def test_a_supermarket_is_not_a_meal(place, out):
    assert agent._not_food(place) == out


def test_a_burger_chain_gives_way_to_a_local_place_nearby():
    t = trip()
    t.keep("q:local", {"name": "Restoran Yasmin", "lat": 51.5001, "lon": -0.12, "halal_level": "likely"}, "qloo", "halal")
    day = {"date": "2026-10-17", "stops": [{"time": "18:00", "kind": "sight", "name": "Park", "lat": 51.5, "lon": -0.1201},
                                          {"time": "19:00", "kind": "meal", "ref": "q:jb", "name": "Jollibee", "lat": 51.5, "lon": -0.12,
                                           "halal_level": "likely"}]}
    asyncio.run(agent._check_meals({"days": [day]}, t))
    assert day["stops"][1]["name"] == "Restoran Yasmin"


@pytest.mark.parametrize("place, out", [
    ({"name": "Chaiiwala", "cuisine": "coffee_shop", "kind": "cafe"}, True),
    ({"name": "Göçmen Artisan Bakery Galata", "categories": ["restaurant", "bakery", "cafe"]}, True),
    ({"name": "Rose Papillon", "categories": ["cafeteria", "restaurant", "coffee shop"]}, True),
    ({"name": "Corner Cafe", "kind": "cafe"}, True),
    ({"name": "Beltaş Cafe and Restaurant", "cuisine": "coffee_shop", "kind": "cafe"}, False),
    ({"name": "Hiba Express", "cuisine": "kebab, lebanese", "kind": "restaurant"}, False),
    ({"name": "Deli", "cuisine": "cake, coffee_shop, sandwich", "kind": "cafe"}, False),
    ({"name": "Kwik Meal", "categories": ["halal restaurant", "restaurant"]}, False),
    ({"name": "Hawksmoor", "categories": ["steakhouse"]}, False),
    ({"name": "Nameless"}, False),
])
def test_a_coffee_shop_or_a_bakery_is_a_snack(place, out):
    assert agent._light(place) == out


def test_a_real_restaurant_before_a_nearer_coffee_shop():
    cafe = {"name": "Chaiiwala", "cuisine": "coffee_shop", "kind": "cafe", "halal_level": "verified", "distance_m": 100}
    grill = {"name": "Hiba Express", "cuisine": "kebab, lebanese", "kind": "restaurant", "halal_level": "verified", "distance_m": 450}
    assert agent._meal_score(grill, "gb") < agent._meal_score(cafe, "gb")


def test_dinner_at_a_coffee_shop_gives_way_to_a_restaurant_nearby():
    t = trip()
    t.keep("osm:chai", {"name": "Chaiiwala", "lat": 51.5, "lon": -0.12, "halal_level": "verified", "cuisine": "coffee_shop",
                        "kind": "cafe"}, "osm", "halal")
    t.keep("osm:cake", {"name": "Sweet Corner", "lat": 51.5002, "lon": -0.12, "halal_level": "verified",
                        "cuisine": "cake", "kind": "cafe"}, "osm", "halal")
    t.keep("osm:hiba", {"name": "Hiba Express", "lat": 51.503, "lon": -0.12, "halal_level": "verified",
                        "cuisine": "kebab, lebanese", "kind": "restaurant"}, "osm", "halal")
    meal = lambda at: {"time": at, "kind": "meal", "ref": "osm:chai", "name": "Chaiiwala", "lat": 51.5, "lon": -0.12,
                       "halal_level": "verified", "cuisine": "coffee_shop", "place_kind": "cafe"}
    walk = {"time": "17:00", "kind": "sight", "name": "Park", "lat": 51.5, "lon": -0.1201}
    day = {"date": "2026-10-17", "stops": [walk, meal("18:30")]}
    asyncio.run(agent._check_meals({"days": [day]}, t))
    assert day["stops"][1]["name"] == "Hiba Express"  # not the cake shop next door
    # breakfast or an afternoon tea at a coffee shop is fine
    day = {"date": "2026-10-17", "stops": [dict(walk, time="09:00"), meal("10:00")]}
    asyncio.run(agent._check_meals({"days": [day]}, t))
    assert day["stops"][1]["name"] == "Chaiiwala"
