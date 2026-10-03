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


def test_mock_qloo_without_key(monkeypatch):
    monkeypatch.setattr(qloo.config, "QLOO_MOCK", True)
    found = asyncio.run(qloo.search("Orhan Pamuk"))
    assert found and found[0]["name"] == "Orhan Pamuk"
