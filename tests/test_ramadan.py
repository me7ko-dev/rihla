"""Prayer times without AlAdhan, and Ramadan: fasting days have no meals before Maghrib, an iftar after it and
Isha with Taraweeh. Run: pytest -q"""
import asyncio
import datetime as dt

import pytest

from app import agent, prayer


def _m(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


# ---------- prayer times calculated on the server ----------
# AlAdhan's answers for the example plans (17 October 2026), one city per calculation method
@pytest.mark.parametrize("lat, lon, country, aladhan", [
    (41.0054, 28.9768, "tr", {"Fajr": "05:46", "Sunrise": "07:10", "Dhuhr": "12:54", "Asr": "15:58", "Maghrib": "18:28", "Isha": "19:47"}),
    (51.5074, -0.1278, "gb", {"Fajr": "05:52", "Sunrise": "07:28", "Dhuhr": "12:46", "Asr": "15:31", "Maghrib": "18:03", "Isha": "19:26"}),
    (40.7831, -73.9712, "us", {"Fajr": "05:54", "Sunrise": "07:09", "Dhuhr": "12:41", "Asr": "15:45", "Maghrib": "18:12", "Isha": "19:28"}),
])
def test_calculated_times_match_aladhan(lat, lon, country, aladhan):
    out = prayer.calculate(lat, lon, dt.date(2026, 10, 17), prayer.METHOD[country])
    for name, t in aladhan.items():
        assert abs(_m(out["timings"][name]) - _m(t)) <= 2, name
    assert out["hijri"] == "6 Jumādá al-ūlá 1448" and not out["ramadan"]


def test_hijri_calendar_knows_ramadan_and_eid():
    first = prayer.calculate(21.42, 39.83, dt.date(2027, 2, 8), 4)
    assert first["hijri"].startswith("1 Ramaḍān") and first["ramadan"] and first["timezone"] == "Asia/Riyadh"
    assert prayer.calculate(21.42, 39.83, dt.date(2027, 3, 9), 4)["eid"] == "Eid al-Fitr"


def test_far_north_summer_still_has_fajr_and_isha():
    out = prayer.calculate(59.33, 18.07, dt.date(2026, 6, 21), 3)   # Stockholm: the sun never reaches 18° below
    t = {k: _m(v) for k, v in out["timings"].items()}
    assert t["Fajr"] < t["Sunrise"] < t["Dhuhr"] < t["Asr"] < t["Maghrib"] < t["Isha"]


def test_plan_survives_aladhan_being_down(monkeypatch):
    async def down(*args):
        raise ConnectionError("api.aladhan.com unreachable")
    monkeypatch.setattr(prayer, "_aladhan", down)
    monkeypatch.setattr(prayer, "_cache", {})
    out = asyncio.run(prayer.times(51.5074, -0.1278, dt.date(2026, 10, 17), "gb"))
    assert out["calculated"] and out["timings"]["Maghrib"] == "18:03"


# ---------- Ramadan ----------
TIMES = {"Fajr": "05:30", "Sunrise": "06:50", "Dhuhr": "12:35", "Asr": "15:50", "Maghrib": "18:20", "Isha": "19:50"}


def _trip():
    trip = agent.Trip({"lat": 21.42, "lon": 39.83, "country_code": "sa", "name": "Makkah"})
    trip.keep("q:sight", {"name": "Museum", "lat": 21.421, "lon": 39.83}, "qloo", "sights")
    trip.keep("q:cafe", {"name": "Bean Bar", "lat": 21.421, "lon": 39.831, "categories": ["coffee shop"]}, "qloo", "sights")
    trip.keep("q:food", {"name": "Al Baik", "lat": 21.422, "lon": 39.83, "halal_level": "likely"}, "qloo", "halal")
    trip.keep("q:food2", {"name": "Mandi House", "lat": 21.423, "lon": 39.83, "halal_level": "verified"}, "qloo", "halal")
    trip.keep("q:mosque", {"name": "Big Mosque", "lat": 21.42, "lon": 39.832, "kind": "mosque"}, "qloo", "mosques")
    return trip


def _day(ramadan: bool):
    trip = _trip()
    stops = [agent._fill_stop({"time": "10:00", "kind": "sight", "ref": "q:sight"}, trip.places["q:sight"]),
             agent._fill_stop({"time": "13:00", "kind": "meal", "ref": "q:food"}, trip.places["q:food"]),
             agent._fill_stop({"time": "16:30", "kind": "rest", "ref": "q:cafe"}, trip.places["q:cafe"])]
    final = {"days": [{"date": "2027-02-15", "prayer_times": TIMES, "ramadan": ramadan, "stops": stops}], "tips": []}
    agent._polish(final, trip)
    return final


def test_fasting_day_has_iftar_and_taraweeh_but_no_lunch():
    final = _day(ramadan=True)
    stops = final["days"][0]["stops"]
    meals = [s for s in stops if s["kind"] == "meal"]
    assert meals and all(_m(s["time"]) >= _m(TIMES["Maghrib"]) for s in meals)    # nothing to eat before Maghrib
    assert meals[0].get("iftar")
    assert not any(s.get("ref") == "q:cafe" for s in stops)                          # no coffee during the fast
    isha = [s for s in stops if s.get("prayer") == "Isha"]
    assert len(isha) == 1 and isha[0]["taraweeh"] and isha[0]["time"] == TIMES["Isha"]
    assert final["tips"] and "Ramadan" in final["tips"][0]


def test_ordinary_day_keeps_lunch_and_has_no_taraweeh():
    stops = _day(ramadan=False)["days"][0]["stops"]
    assert any(s["kind"] == "meal" and _m(s["time"]) < 15 * 60 for s in stops)
    assert not any(s.get("taraweeh") or s.get("iftar") for s in stops)


def test_ramadan_tip_in_another_language_is_enough():
    final = {"days": [{"ramadan": True, "prayer_times": TIMES, "stops": []}], "tips": ["Резервирайте маса за ифтар."]}
    agent._iftar(final)
    assert final["tips"] == ["Резервирайте маса за ифтар."]


def test_isha_written_by_the_model_is_kept_in_ramadan():
    assert agent._prayer_name("19:45", TIMES, isha=True) == "Isha"
    assert agent._prayer_name("19:45", TIMES) == "Maghrib"
