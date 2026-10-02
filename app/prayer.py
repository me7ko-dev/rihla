"""Prayer times from the free AlAdhan API (aladhan.com)."""
import datetime as dt
import time

import httpx

from .config import USER_AGENT

_cache: dict[str, tuple[float, dict]] = {}
PRAYERS = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]

# Calculation method by country (AlAdhan ids). Default 3 = Muslim World League.
METHOD = {"tr": 13, "sa": 4, "ae": 16, "eg": 5, "pk": 1, "in": 1, "ir": 7, "kw": 9, "qa": 10, "sg": 11,
          "fr": 12, "ru": 14, "my": 17, "tn": 18, "dz": 19, "id": 20, "ma": 21, "pt": 22, "jo": 23,
          "us": 2, "ca": 2, "gb": 15, "bg": 13}


async def times(lat: float, lon: float, date: dt.date, country_code: str = "") -> dict:
    """{date, hijri, timings: {Fajr: 'HH:MM', ...}} for that day."""
    method = METHOD.get((country_code or "").lower(), 3)
    key = f"{lat:.2f}:{lon:.2f}:{date.isoformat()}:{method}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 24 * 3600:
        return hit[1]
    url = f"https://api.aladhan.com/v1/timings/{date.strftime('%d-%m-%Y')}"
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=20) as c:
        r = await c.get(url, params={"latitude": lat, "longitude": lon, "method": method})
    r.raise_for_status()
    d = r.json()["data"]
    h = d["date"]["hijri"]
    out = {
        "date": date.isoformat(),
        "hijri": f'{h["day"]} {h["month"]["en"]} {h["year"]}',
        "timings": {p: d["timings"][p][:5] for p in PRAYERS} | {"Sunrise": d["timings"]["Sunrise"][:5]},
        "method": d.get("meta", {}).get("method", {}).get("name", ""),
    }
    _cache[key] = (time.time(), out)
    return out
