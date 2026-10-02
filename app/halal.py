"""How sure can we be that a place serves halal food?

verified - listed as halal: tagged diet:halal=yes|only on OpenStreetMap (or matched by name+distance to such
           a place), or a Qloo place in the "halal restaurant" category
likely   - diners mention halal there (Qloo tags), a Muslim-majority country, or a cuisine that is usually
           halal (Turkish, Lebanese, Pakistani...)
unknown  - no signal; the traveller should ask
We never claim more than the data supports.
"""
import re
import unicodedata
from difflib import SequenceMatcher

from .osm import distance_m

# ISO-2 codes of Muslim-majority countries
MUSLIM_MAJORITY = set("""af al az bh bd bn km dj eg gm gn id ir iq jo kz xk kw kg lb ly mv ml mr ma ne om pk ps qa sa sn
so sd sy tj tn tr tm ae uz ye ba""".split())
HALAL_CUISINES = ("turkish", "lebanese", "middle_eastern", "arab", "pakistani", "afghan", "persian", "iranian",
                  "syrian", "moroccan", "uzbek", "yemeni", "egyptian", "bangladeshi", "malaysian", "indonesian",
                  "kebab", "shawarma", "falafel", "palestinian", "iraqi", "somali", "halal", "doner", "döner")


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(restaurant|restoran|cafe|kebab house|grill|the)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def level(place: dict, country_code: str = "") -> tuple[str, str]:
    """(level, reason) for an OSM or Qloo place dict."""
    if place.get("halal") in ("yes", "only"):
        return "verified", "Tagged halal on OpenStreetMap" + (" (halal only)" if place.get("halal") == "only" else "")
    if place.get("qloo_halal") == "listed":
        return "verified", "Listed as a halal restaurant (Qloo)"
    if place.get("alcohol"):
        return "unknown", "Serves alcohol — ask whether the meat is halal"
    if (country_code or "").lower() in MUSLIM_MAJORITY:
        return "likely", "Muslim-majority country: most food is halal, but ask if unsure"
    if place.get("qloo_halal") == "mentioned":
        return "likely", "Diners mention halal food here (Qloo) — please confirm when ordering"
    text = " ".join([place.get("cuisine", ""), place.get("name", "")] + place.get("categories", [])).lower()
    if any(c in text for c in HALAL_CUISINES):
        return "likely", "Cuisine that is usually halal — please confirm with the restaurant"
    return "unknown", "No halal information — ask before ordering"


def match_verified(item: dict, osm_halal: list[dict], max_m: int = 200) -> dict | None:
    """Find the OSM halal-tagged place that is the same venue as a Qloo restaurant."""
    n = _norm(item.get("name", ""))
    if not n:
        return None
    best, score = None, 0.0
    for p in osm_halal:
        if item.get("lat") and p.get("lat") and distance_m(item["lat"], item["lon"], p["lat"], p["lon"]) > max_m:
            continue
        s = SequenceMatcher(None, n, _norm(p["name"])).ratio()
        if s > score:
            best, score = p, s
    return best if score >= 0.82 else None
