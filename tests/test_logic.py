"""Offline tests for the rules that keep Rihla honest: halal levels, no alcohol venues, only names the traveller
wrote become taste signals, prayers and opening hours respected, no overlapping stops, no leaked keys.
Run: pytest -q"""
import asyncio
import json

import pytest
from fastapi import HTTPException

from app import agent, halal, main, qloo


# ---------- halal confidence ----------
@pytest.mark.parametrize("place, country, level", [
    ({"halal": "yes"}, "gb", "verified"),                                       # tagged on OpenStreetMap
    ({"qloo_halal": "listed"}, "us", "verified"),                               # a halal restaurant in Qloo
    ({"qloo_halal": "mentioned", "alcohol": True}, "gb", "unknown"),            # reviews say halal, but a full bar
    ({"name": "Kebab Palace"}, "tr", "likely"),                                 # Muslim-majority country
    ({"qloo_halal": "mentioned"}, "fr", "likely"),                              # diners mention halal
    ({"cuisine": "lebanese", "name": "Cedar"}, "gb", "likely"),                 # usually halal cuisine
    ({"name": "Le Bistro", "cuisine": "french"}, "fr", "unknown"),              # no signal: ask
])
def test_halal_levels(place, country, level):
    assert halal.level(place, country)[0] == level


def test_alcohol_venues_are_recognised():
    assert qloo.is_alcohol_venue({"name": "Somewhere", "tag_ids": ["urn:tag:category:place:pub"]})
    assert qloo.is_alcohol_venue({"name": "The Red Lion Pub", "tag_ids": []})
    assert qloo.is_alcohol_venue({"name": "Kör Agop", "tag_ids": ["urn:tag:cuisine:qloo:meyhane"]})
    assert not qloo.is_alcohol_venue({"name": "Barbican Centre", "tag_ids": []})   # "bar" inside a word
    assert qloo.serves_alcohol({"tag_ids": ["urn:tag:offerings:place:alcohol"]})


def test_halal_signal_listed_vs_mentioned():
    assert qloo.halal_signal({"tag_ids": ["urn:tag:category:place:halal_restaurant"]}) == "listed"
    assert qloo.halal_signal({"tag_ids": ["urn:tag:dietary_option:qloo:halal"]}) == "mentioned"
    assert qloo.halal_signal({"tag_ids": ["urn:tag:category:place:restaurant"]}) == ""


# ---------- taste signals: only names the traveller wrote ----------
TEXT = "We love history, calligraphy, Orhan Pamuk novels, sea views and street food."


@pytest.mark.parametrize("query, named", [
    ("Orhan Pamuk", True), ("history", False), ("sea views", False),
    ("Bruce Springsteen", False),            # a name the model made up
])
def test_only_written_names_become_signals(query, named):
    assert agent._named(TEXT, query) is named


def test_lower_case_text_gives_no_capital_hint():
    assert agent._named("we love harry potter and pixar", "harry potter")


def test_name_endings_are_dropped():
    assert agent._variants("Orhan Pamuk'un romanları")[-1] == "Orhan Pamuk"


def test_keywords_keep_the_travellers_capitals():
    assert agent._as_written("We love the Harry Potter books", "harry potter") == "Harry Potter"


@pytest.mark.parametrize("travellers, kids", [
    ("Family of four, kids aged 7 and 11", True), ("Mother and daughter (16)", False),
    ("Couple with a baby", True), ("Couple in their 30s", False),
    ("Parents with a 4-year-old", True), ("Me and my 6 years old", True), ("Two adults and a 3 yo", True),  # no "kids"
    ("Couple, married 3 years", False), ("Grandparents, 70 years old", False), ("Two adults aged 35 and 38", False),
    ("Семейство с деца на 4 и 6 години", True), ("Майка с бебе", True), ("Родители с 5-годишно", True),
    ("Двама възрастни", False),
])
def test_young_children_audience(travellers, kids):
    assert agent.Trip({"lat": 0, "lon": 0}, travellers, "").kids is kids


# ---------- the day plan ----------
def test_no_overlapping_stops_and_prayers_stay_put():
    final = {"days": [{"stops": [
        {"time": "11:00", "kind": "sight", "name": "A"},
        {"time": "11:00", "kind": "sight", "name": "B"},
        {"time": "12:46", "kind": "prayer", "name": "Mosque"},
    ]}]}
    agent._untangle(final)
    times = {s["name"]: s["time"] for s in final["days"][0]["stops"]}
    assert times["A"] == "11:00" and times["B"] == "12:15" and times["Mosque"] == "12:46"


@pytest.mark.parametrize("text, mins", [
    ("13:30", 810), ("9:30", 570), ("1:30 PM", 810), ("12:15 am", 15), ("13h30", 810), ("noon", None), ("25:00", None),
])
def test_times_as_the_model_writes_them(text, mins):
    assert agent._mins(text) == mins


def test_no_visit_during_or_just_before_a_prayer():
    final = {"days": [{"stops": [
        {"time": "15:25", "kind": "sight", "name": "Zoo"},          # 19 min before Asr: after the prayer instead
        {"time": "15:44", "kind": "prayer", "name": "Mosque"},
        {"time": "18:00", "kind": "sight", "name": "Fountain"},     # would start during Maghrib
        {"time": "18:03", "kind": "prayer", "name": "Mosque 2"},
        {"time": "23:00", "kind": "meal", "name": "Late"},          # too late to happen: dropped
    ]}]}
    agent._untangle(final)
    times = {s["name"]: s["time"] for s in final["days"][0]["stops"]}
    assert times == {"Zoo": "16:05", "Mosque": "15:44", "Fountain": "18:25", "Mosque 2": "18:03"}


def test_no_park_after_sunset_and_no_second_dinner():
    final = {"days": [{"prayer_times": {"Maghrib": "18:11"}, "stops": [
        {"time": "16:30", "kind": "sight", "name": "Central Park Zoo", "categories": ["zoo"]},
        {"time": "17:00", "kind": "sight", "name": "Bethesda Fountain"},      # pushed past sunset by the zoo
        {"time": "18:11", "kind": "prayer", "prayer": "Maghrib", "name": "Mosque"},
        {"time": "19:00", "kind": "meal", "name": "Dinner"},
        {"time": "20:00", "kind": "meal", "name": "Second dinner"},
        {"time": "19:30", "kind": "sight", "name": "Museum at night"},        # indoors: stays
        {"time": "21:00", "kind": "sight", "name": "Madison Square Garden", "categories": ["stadium"]},
    ]}]}
    agent._untangle(final)
    assert [s["name"] for s in final["days"][0]["stops"]] == ["Central Park Zoo", "Mosque", "Dinner", "Museum at night",
                                                              "Madison Square Garden"]


def test_a_visit_moved_past_closing_time_is_left_out():
    final = {"days": [{"stops": [
        {"time": "15:25", "kind": "sight", "name": "Zoo", "open_today": "10:00–16:30"},   # after Asr it is closing
        {"time": "15:44", "kind": "prayer", "name": "Mosque"},
    ]}]}
    agent._untangle(final)
    assert [s["name"] for s in final["days"][0]["stops"]] == ["Mosque"]


def test_plan_goes_on_without_prayer_times(monkeypatch):
    async def down(*a, **k):
        raise RuntimeError("503 Service Unavailable")
    monkeypatch.setattr(agent.prayer, "times", down)
    day = asyncio.run(agent._prayer_times({"lat": 51.5, "lon": -0.12, "country_code": "gb"}, agent.dt.date(2026, 10, 17)))
    assert day["date"] == "2026-10-17" and day["timings"] == {} and day["missing"]


def test_no_language_model_key_is_a_clean_failure(monkeypatch):
    monkeypatch.setattr(agent.config, "LLM_API_KEY", "")
    monkeypatch.setattr(agent.config, "FALLBACK_API_KEY", "")
    with pytest.raises(RuntimeError):   # plan() catches this and assembles the plan itself
        asyncio.run(agent.LLM().chat(messages=[]))


def test_the_model_is_not_called_without_time_left(monkeypatch):
    monkeypatch.setattr(agent.config, "LLM_API_KEY", "nvapi-test")
    llm = agent.LLM(deadline=agent.time.time() + 15)
    with pytest.raises(RuntimeError):
        asyncio.run(llm.chat(messages=[]))


@pytest.mark.parametrize("query, found, same", [
    ("Sultanahmet, Istanbul", "Istanbul", True), ("istambul", "Istanbul", True), ("London", "London", True),
    ("Lodnon", "London", True), ("Qwxzv", "Quezon City", False), ("Paris", "Parma", False),
])
def test_qloo_city_must_match_what_was_written(query, found, same):
    assert qloo.same_place(query, found) == same


def _at(km_north: float) -> dict:
    return {"lat": 51.5 + km_north / 111.2, "lon": -0.12}   # London, km_north km (straight line) up the map


@pytest.mark.parametrize("km_north, kids, minutes", [
    (0.923, False, 16), (0.923, True, 20),     # 1.2 km of streets: 16 min for adults, 20 min with young children
    (1.38, False, 24),                         # 1.8 km of streets: still a walk for adults (24 min)...
    (1.38, True, 20),                          # ...but too far for small children (30 min): metro or taxi
    (5.0, False, 34),                          # 6.5 km of streets: metro or taxi
])
def test_travel_time_between_stops(km_north, kids, minutes):
    assert abs(agent._travel_min(_at(0), _at(km_north), kids) - minutes) <= 1


def test_next_stop_waits_for_the_walk():
    def day():
        return {"days": [{"stops": [
            {"time": "10:00", "kind": "sight", "name": "A", **_at(0)},
            {"time": "11:15", "kind": "meal", "name": "B", **_at(0.6)},      # 0.78 km of streets
            {"time": "14:00", "kind": "sight", "name": "C", **_at(0.9)},     # enough time already: stays
            {"time": "12:46", "kind": "prayer", "name": "Mosque", **_at(3)},  # prayers keep their time
        ]}]}
    for kids, lunch in ((False, "11:25"), (True, "11:30")):              # 10 min on foot, 13 min with small children
        final = day()
        agent._untangle(final, kids)
        times = {s["name"]: s["time"] for s in final["days"][0]["stops"]}
        assert times == {"A": "10:00", "B": lunch, "C": "14:00", "Mosque": "12:46"}


class _Trip:
    dest = {"lat": 48.86, "lon": 2.35}

    def __init__(self, places=None):
        self.places = places or {}

    def near(self, *args):
        return []


def test_jumuah_on_friday_moves_overlapping_visits():
    day = {"date": "2026-10-16", "stops": [
        {"time": "13:00", "kind": "sight", "name": "59 Rivoli", "lat": 48.86, "lon": 2.34},
        {"time": "13:36", "kind": "prayer", "prayer": "Dhuhr", "name": "Grande Mosquée", "lat": 48.84, "lon": 2.355, "ref": "m"},
    ]}
    agent._jumuah({"days": [day]}, _Trip())
    prayer = next(s for s in day["stops"] if s["kind"] == "prayer")
    visit = next(s for s in day["stops"] if s["kind"] == "sight")
    assert prayer.get("jumuah") and visit["time"] == "14:30"


def test_opening_hours():
    museum = {"hours": {"saturday": [{"opens": "T10:00:00", "closes": "T17:50:00", "closed": False}],
                        "sunday": [{"opens": None, "closes": None, "closed": True}]}}
    final = {"days": [
        {"date": "2026-10-17", "stops": [{"time": "09:00", "kind": "sight", "ref": "q:1"}]},   # Saturday: too early
        {"date": "2026-10-18", "stops": [{"time": "11:00", "kind": "sight", "ref": "q:1"}]},   # Sunday: closed
    ]}
    agent._check_hours(final, _Trip({"q:1": museum}))
    assert final["days"][0]["stops"][0]["time"] == "10:00"
    assert final["days"][1]["stops"] == []


def test_route_removes_zigzag():
    west, east = (51.50, -0.18), (51.51, -0.08)
    day = {"stops": [
        {"time": "10:00", "kind": "sight", "name": "W1", "lat": west[0], "lon": west[1]},
        {"time": "11:30", "kind": "sight", "name": "E1", "lat": east[0], "lon": east[1]},
        {"time": "14:00", "kind": "sight", "name": "W2", "lat": west[0] + 0.002, "lon": west[1]},
        {"time": "15:30", "kind": "sight", "name": "E2", "lat": east[0] + 0.002, "lon": east[1]},
    ]}
    agent._route({"days": [day]})
    assert [s["name"] for s in day["stops"]] == ["W1", "W2", "E1", "E2"]


def test_tool_results_are_trimmed_not_cut():
    result = {"museum": [{"name": "x" * 50} for _ in range(100)], "park": [{"name": "y"}]}
    text = agent._fit(result, 2000)
    assert len(text) <= 2000 and json.loads(text)["park"]


# ---------- the model's plan is checked before it is used ----------
def test_stray_words_in_the_model_plan_are_dropped():
    stop = {"time": "09:00", "kind": "sight", "ref": "qloo:1", "why": "Dinosaurs"}
    final = {"title": "t", "days": [{"day": 1, "stops": [stop, "oops"]}, "tips"], "tips": "Bring water"}  # seen 04.10
    assert agent._usable(final, 1)
    assert final["days"] == [{"day": 1, "stops": [stop]}] and final["tips"] == ["Bring water"]


@pytest.mark.parametrize("final, days", [
    (None, 1), ([{"day": 1}], 1), ({"days": "Day 1"}, 1), ({"days": []}, 1),
    ({"days": [{"day": 1, "stops": []}]}, 1),                     # a day without stops
    ({"days": [{"day": 1, "stops": [{"time": "09:00"}]}]}, 2),    # one day of two: ask again or plan without the model
])
def test_unusable_model_plans_are_refused(final, days):
    assert not agent._usable(final, days)


def test_tips_filed_inside_a_day_are_lifted():
    final = {"days": [{"day": 1, "stops": [{"time": "09:00"}], "tips": ["Carry a prayer mat", 3]}]}  # seen 04.10
    assert agent._usable(final, 1) and final["tips"] == ["Carry a prayer mat"] and "tips" not in final["days"][0]


@pytest.mark.parametrize("tips, kept", [
    ("Buy a Museum Pass. Wear comfortable shoes.", ["Buy a Museum Pass. Wear comfortable shoes."]),  # one text, seen 04.10
    ("Buy a Museum Pass\n- \nWear comfortable shoes", ["Buy a Museum Pass", "-", "Wear comfortable shoes"]),
    ("Buy a Museum Pass; wear comfortable shoes", ["Buy a Museum Pass", "wear comfortable shoes"]),
    (None, []), (7, []),
])
def test_tips_written_as_text(tips, kept):
    final = {"days": [{"day": 1, "stops": [{"time": "09:00"}]}], "tips": tips}
    assert agent._usable(final, 1) and final["tips"] == kept


def test_extra_days_are_cut():
    final = {"days": [{"day": n, "stops": [{"time": "09:00"}]} for n in (1, 2, 3)]}
    assert agent._usable(final, 2) and [d["day"] for d in final["days"]] == [1, 2]


# ---------- the web app ----------
def test_keys_never_leave_the_server(monkeypatch):
    monkeypatch.setattr(main, "_SECRETS", ["hack_secret_value_123"])
    out = main._clean("401 Incorrect key hack_secret_value_123 / nvapi-abcdefghijkl / gsk_abcdefghijkl")
    assert "secret" not in out and "nvapi-" not in out and "gsk_" not in out


def test_fair_use_limit(monkeypatch):
    monkeypatch.setattr(main, "_by_ip", main.defaultdict(main.deque))

    class Req:
        headers = {"x-forwarded-for": "203.0.113.7"}
        client = None
    for _ in range(main.PER_IP_HOUR):
        main._allow(Req())
    with pytest.raises(HTTPException) as e:
        main._allow(Req())
    assert e.value.status_code == 429


def test_failed_plans_do_not_use_up_the_limit(monkeypatch):
    monkeypatch.setattr(main, "_by_ip", main.defaultdict(main.deque))

    class Req:
        headers = {"x-vercel-forwarded-for": "203.0.113.8", "x-forwarded-for": "198.51.100.1"}
        client = None
    for _ in range(main.PER_IP_HOUR * 2):   # e.g. a typo in the city, again and again
        main._allow(Req())
        main._refund(Req())
    main._allow(Req())
    assert len(main._by_ip["203.0.113.8"]) == 1 and not main._by_ip["198.51.100.1"]


def test_mock_qloo_without_key(monkeypatch):
    monkeypatch.setattr(qloo.config, "QLOO_MOCK", True)
    found = asyncio.run(qloo.search("Orhan Pamuk"))
    assert found and found[0]["name"] == "Orhan Pamuk"
