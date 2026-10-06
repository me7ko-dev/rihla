"""Prayer times from the free AlAdhan API (aladhan.com), worked out on the server itself if AlAdhan is down,
so a plan never fails because of it."""
import datetime as dt
import logging
import math
import time
from zoneinfo import ZoneInfo

import httpx
from hijridate import Gregorian
from tzfpy import get_tz

from .config import USER_AGENT

log = logging.getLogger("rihla")
_cache: dict[str, tuple[float, dict]] = {}
PRAYERS = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]
RAMADAN = 9  # Hijri month number
EID = {(10, 1): "Eid al-Fitr", (12, 10): "Eid al-Adha"}  # (Hijri month, day)

# Calculation method by country (AlAdhan ids). Default 3 = Muslim World League.
METHOD = {"tr": 13, "sa": 4, "ae": 16, "eg": 5, "pk": 1, "in": 1, "ir": 7, "kw": 9, "qa": 10, "sg": 11,
          "fr": 12, "ru": 14, "my": 17, "tn": 18, "dz": 19, "id": 20, "ma": 21, "pt": 22, "jo": 23,
          "us": 2, "ca": 2, "gb": 15, "bg": 13}

# The same methods for the local calculation: (name, Fajr angle, Isha angle or minutes after Maghrib as "90 min")
METHODS = {1: ("University of Islamic Sciences, Karachi", 18, 18), 2: ("Islamic Society of North America", 15, 15),
           3: ("Muslim World League", 18, 17), 4: ("Umm Al-Qura University, Makkah", 18.5, "90 min"),
           5: ("Egyptian General Authority of Survey", 19.5, 17.5), 7: ("Institute of Geophysics, University of Tehran", 17.7, 14),
           9: ("Kuwait", 18, 17.5), 10: ("Qatar", 18, "90 min"), 11: ("Majlis Ugama Islam Singapura", 20, 18),
           12: ("Union Organization Islamic de France", 12, 12), 13: ("Diyanet İşleri Başkanlığı, Turkey", 18, 17),
           14: ("Spiritual Administration of Muslims of Russia", 16, 15), 15: ("Moonsighting Committee Worldwide", 18, 18),
           16: ("Dubai", 18.2, 18.2), 17: ("Jabatan Kemajuan Islam Malaysia (JAKIM)", 20, 18), 18: ("Tunisia", 18, 18),
           19: ("Algeria", 18, 17), 20: ("Kementerian Agama Republik Indonesia", 20, 18), 21: ("Morocco", 19, 17),
           22: ("Comunidade Islamica de Lisboa", 18, "77 min"), 23: ("Ministry of Awqaf, Jordan", 18, 18)}
TEHRAN_MAGHRIB = 4.5  # method 7: Maghrib when the sun is 4.5° below the horizon, not at sunset
# Diyanet's fixed corrections in minutes, as AlAdhan applies them for Turkey
DIYANET = {"Sunrise": -7, "Dhuhr": 5, "Asr": 4, "Maghrib": 7}
# AlAdhan's English month names, so both sources read the same
HIJRI_MONTHS = ["Muḥarram", "Ṣafar", "Rabīʿ al-awwal", "Rabīʿ al-thānī", "Jumādá al-ūlá", "Jumādá al-ākhirah", "Rajab",
                "Shaʿbān", "Ramaḍān", "Shawwāl", "Dhū al-Qaʿdah", "Dhū al-Ḥijjah"]


async def times(lat: float, lon: float, date: dt.date, country_code: str = "") -> dict:
    """{date, hijri, hijri_month, ramadan, timings: {Fajr: 'HH:MM', ...}, timezone} for that day (times are local
    to the place). From AlAdhan; if it cannot be reached, calculated here with the same method."""
    method = METHOD.get((country_code or "").lower(), 3)
    key = f"{lat:.2f}:{lon:.2f}:{date.isoformat()}:{method}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 24 * 3600:
        return hit[1]
    try:
        out = await _aladhan(lat, lon, date, method)
    except Exception as e:  # down, slow or changed: the sun is still where it was
        log.warning("AlAdhan unavailable (%s): calculating prayer times locally", type(e).__name__)
        return calculate(lat, lon, date, method)
    _cache[key] = (time.time(), out)
    return out


async def _aladhan(lat: float, lon: float, date: dt.date, method: int) -> dict:
    url = f"https://api.aladhan.com/v1/timings/{date.strftime('%d-%m-%Y')}"
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=12) as c:
        r = await c.get(url, params={"latitude": lat, "longitude": lon, "method": method})
    r.raise_for_status()
    d = r.json()["data"]
    h = d["date"]["hijri"]
    month = int(h["month"]["number"])
    return {
        "date": date.isoformat(),
        "hijri": f'{h["day"]} {h["month"]["en"]} {h["year"]}',
        "hijri_month": month,
        "ramadan": month == RAMADAN,
        "eid": EID.get((month, int(h["day"])), ""),
        "timings": {p: d["timings"][p][:5] for p in PRAYERS} | {"Sunrise": d["timings"]["Sunrise"][:5]},
        "method": d.get("meta", {}).get("method", {}).get("name", ""),
        "timezone": d.get("meta", {}).get("timezone", ""),
    }


# ---------- local calculation (the PrayTimes.org formulas that AlAdhan is based on) ----------
def _sun(jd: float) -> tuple[float, float]:
    """(declination in degrees, equation of time in hours) at Julian day jd."""
    d = jd - 2451545.0
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    lam = math.radians((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360)
    e = math.radians(23.439 - 0.00000036 * d)
    ra = (math.degrees(math.atan2(math.cos(e) * math.sin(lam), math.cos(lam))) / 15) % 24
    eqt = q / 15 - ra
    eqt = (eqt + 12) % 24 - 12
    return math.degrees(math.asin(math.sin(e) * math.sin(lam))), eqt


def calculate(lat: float, lon: float, date: dt.date, method: int = 3) -> dict:
    """The day's prayer times without any network call: sun position for the date, the method's Fajr and Isha
    angles, Asr when a shadow equals its object (standard), and the local time zone from the coordinates."""
    name, fajr_angle, isha = METHODS.get(method, METHODS[3])
    tz = get_tz(lon, lat) or "UTC"
    offset = (ZoneInfo(tz).utcoffset(dt.datetime(date.year, date.month, date.day, 12)) or dt.timedelta()).total_seconds() / 3600
    jd = date.toordinal() + 1721424.5 - lon / 360  # Julian day at local midnight
    phi = math.radians(lat)

    def noon(t: float) -> float:
        return (12 - _sun(jd + t / 24)[1]) % 24

    def at_angle(angle: float, t: float, before_noon: bool) -> float:
        decl = math.radians(_sun(jd + t / 24)[0])
        x = (-math.sin(math.radians(angle)) - math.sin(decl) * math.sin(phi)) / (math.cos(decl) * math.cos(phi))
        if not -1 <= x <= 1:
            return math.nan  # the sun never gets that low (summer nights far north)
        h = math.degrees(math.acos(x)) / 15
        return noon(t) + (-h if before_noon else h)

    def asr(t: float) -> float:
        decl = _sun(jd + t / 24)[0]
        angle = -math.degrees(math.atan(1 / (1 + math.tan(math.radians(abs(lat - decl))))))
        return at_angle(angle, t, False)

    first = {"Fajr": 5, "Sunrise": 6, "Dhuhr": 12, "Asr": 13, "Sunset": 18, "Maghrib": 18, "Isha": 18}
    t = dict(first)
    for _ in range(2):  # the second pass uses the sun's position at the first answers
        t = {k: (v if not math.isnan(v) else first[k]) for k, v in t.items()}
        t = {"Fajr": at_angle(fajr_angle, t["Fajr"], True), "Sunrise": at_angle(0.833, t["Sunrise"], True),
             "Dhuhr": noon(t["Dhuhr"]), "Asr": asr(t["Asr"]), "Sunset": at_angle(0.833, t["Sunset"], False),
             "Maghrib": at_angle(TEHRAN_MAGHRIB if method == 7 else 0.833, t["Maghrib"], False),
             "Isha": math.nan if isinstance(isha, str) else at_angle(isha, t["Isha"], False)}
    night = (t["Sunrise"] - t["Sunset"]) % 24
    if method == 15:  # Moonsighting Committee: Fajr no earlier, Isha no later than its seasonal twilight
        fajr_min = _seasonal(lat, date, 28.65, 19.44, 32.74, 48.10)
        isha_min = _seasonal(lat, date, 25.60, 2.050, -9.21, 6.14)
        if math.isnan(t["Fajr"]) or (t["Sunrise"] - t["Fajr"]) * 60 > fajr_min:
            t["Fajr"] = t["Sunrise"] - fajr_min / 60
        if math.isnan(t["Isha"]) or (t["Isha"] - t["Sunset"]) * 60 > isha_min:
            t["Isha"] = t["Sunset"] + isha_min / 60
    # far north in summer: Fajr and Isha by the share of the night their angle stands for (AlAdhan's "angle based")
    if math.isnan(t["Fajr"]) or (t["Sunrise"] - t["Fajr"]) % 24 > fajr_angle / 60 * night:
        t["Fajr"] = t["Sunrise"] - fajr_angle / 60 * night
    if isinstance(isha, str):  # a fixed time after Maghrib, e.g. Umm Al-Qura's 90 minutes
        t["Isha"] = t["Maghrib"] + int(isha.split()[0]) / 60
    elif math.isnan(t["Isha"]) or (t["Isha"] - t["Sunset"]) % 24 > isha / 60 * night:
        t["Isha"] = t["Sunset"] + isha / 60 * night
    if method == 13:
        t = {k: v + DIYANET.get(k, 0) / 60 for k, v in t.items()}

    def clock(h: float, up: bool = True) -> str:
        # a prayer is never shown before its time begins (rounded up); sunrise, when Fajr ends, is rounded down
        m = (h + offset - lon / 15) * 60
        m = (math.ceil(m - 0.2) if up else math.floor(m + 0.2)) % (24 * 60)
        return f"{m // 60:02d}:{m % 60:02d}"

    day, month, year = hijri(date)
    return {
        "date": date.isoformat(),
        "hijri": f"{day} {HIJRI_MONTHS[month - 1]} {year}",
        "hijri_month": month,
        "ramadan": month == RAMADAN,
        "eid": EID.get((month, day), ""),
        "timings": {p: clock(t[p]) for p in PRAYERS} | {"Sunrise": clock(t["Sunrise"], up=False)},
        "method": name,
        "timezone": tz,
        "calculated": True,  # by Rihla, not AlAdhan: the Hijri date can differ from the local moon sighting by a day
    }


def hijri(date: dt.date) -> tuple[int, int, int]:
    """(day, month, year) in the Umm al-Qura calendar, as AlAdhan gives it."""
    h = Gregorian(date.year, date.month, date.day).to_hijri()
    return h.day, h.month, h.year


def _seasonal(lat: float, date: dt.date, a: float, b: float, c: float, d: float) -> float:
    """Moonsighting Committee: twilight in minutes, by latitude and days since the winter solstice."""
    k = abs(lat) / 55
    a, b, c, d = 75 + a * k, 75 + b * k, 75 + c * k, 75 + d * k
    year_days = 366 if date.year % 4 == 0 and (date.year % 100 or date.year % 400 == 0) else 365
    n = (date.timetuple().tm_yday + (10 if lat >= 0 else -(year_days - 365) - 172)) % year_days
    for start, length, x, y in ((0, 91, a, b), (91, 46, b, c), (137, 46, c, d), (183, 46, d, c), (229, 46, c, b)):
        if n < start + length:
            return x + (y - x) / length * (n - start)
    return b + (a - b) / 91 * (n - 275)
