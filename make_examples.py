"""Builds the example plans shown on the home page (web/examples/*.json) with the live agent.
The agent writes a different plan each time: each example is made a few times and the richest plan is kept.
Run after changing the agent:  python make_examples.py [--fresh] [id ...]   (--fresh: do not keep an earlier plan)"""
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

from app import agent
from app.osm import distance_m

OUT = Path(__file__).resolve().parent / "web" / "examples"
TRIES = 3
EXAMPLES = {
    # London starts on a Friday, so the example shows Jumu'ah
    "london-family": dict(destination="London", days=2, weekday=4, travellers="Family of four, kids aged 7 and 11",
                          tastes="We love Pixar movies and the Harry Potter books. The kids are into Minecraft and dinosaurs."),
    "istanbul-couple": dict(destination="Sultanahmet, Istanbul", days=2, travellers="Couple in their 30s",
                            tastes="We love Orhan Pamuk's novels, Sami Yusuf's music, Ottoman history, calligraphy, coffee and sweets."),
    "newyork-father-son": dict(destination="Manhattan, New York", days=2, travellers="Father and son (12)",
                               tastes="We love Marvel movies, Spider-Man, the New York Knicks and science. My son reads Percy Jackson."),
}


def richness(plan: dict, kids: bool = False) -> float:
    """More personal and famous places, fewer stops the code had to add, listed-halal meals (lunch and dinner at a
    restaurant, not a coffee shop), more walks and fewer rides (a family should not take the metro seven times a day)."""
    score = 0.0
    for day in plan["days"]:
        for s in day["stops"]:
            if s["kind"] == "sight":
                score += 1 + 2 * bool(s.get("topic") or s.get("requested")) + bool(s.get("because")) + bool(s.get("popular"))
                score -= 1.5 * bool(s.get("added_by")) + 0.5 * (not s.get("description"))
                score -= 3 * bool(agent.CLOSED_FOR_YEARS.search(s.get("name", "").strip()))  # from before the rule
            elif s["kind"] == "meal":
                t = int(s["time"][:2]) * 60 + int(s["time"][3:5])
                main = 11 * 60 + 30 <= t <= 15 * 60 or t >= 17 * 60
                score += {"verified": 1, "likely": 0.5}.get(s.get("halal_level"), 0) - 0.5 * bool(s.get("alcohol")) \
                    - 2 * (main and agent._light(s))
        pts = [(s["lat"], s["lon"]) for s in day["stops"] if s["kind"] != "prayer" and s.get("lat") is not None]
        score -= 0.4 * sum(max(0, distance_m(*pts[i], *pts[i + 1]) - 2000) for i in range(len(pts) - 1)) / 1000
        score -= 0.6 * sum(distance_m(*pts[i], *pts[i + 1]) * 1.3 / 1000 > agent.WALK[kids][0] for i in range(len(pts) - 1))
    return score


def first_day(weekday: int | None) -> dt.date:
    """About two weeks ahead; on that weekday if one is given (4 = Friday)."""
    day = dt.date.today() + dt.timedelta(days=14)
    while weekday is not None and day.weekday() != weekday:
        day += dt.timedelta(days=1)
    return day


async def warm_photos(plan: dict) -> None:
    """Qloo's image server makes a photo the first time it is asked for (slow, or a 30 px placeholder): ask once
    now, so the photos are ready when someone opens the example."""
    import httpx
    from app import wiki
    urls = {st["image"] for d in plan["days"] for st in d["stops"] if st.get("image")}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        for _ in range(2):
            got = await asyncio.gather(*[c.get(u) for u in urls], return_exceptions=True)
    ok = {u for u, r in zip(urls, got) if not isinstance(r, Exception) and r.status_code == 200 and len(r.content) > 3000}
    for st in (st for d in plan["days"] for st in d["stops"] if st.get("image") and st["image"] not in ok):
        try:  # a placeholder: the Wikipedia photo of the place, or none
            st["image"] = await wiki.photo(st["name"], st["lat"], st["lon"]) or None
        except Exception:
            st["image"] = None
        if not st["image"]:
            st.pop("image")


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fresh = "--fresh" in sys.argv
    for key in [a for a in sys.argv[1:] if not a.startswith("--")] or EXAMPLES:
        ex, best = EXAMPLES[key], None
        kids = bool(agent.KIDS.search(ex["travellers"]))
        rich = lambda p: richness(p, kids)
        start = first_day(ex.get("weekday"))
        for _ in range(TRIES):
            try:
                plan = await agent.plan(ex["destination"], ex["days"], start, ex["travellers"], ex["tastes"])
            except Exception as e:  # a slow model call: try again
                print(f"{key}: failed ({str(e)[:80]})", flush=True)
                continue
            print(f"  {key}: {rich(plan):.1f} — {plan['title']} ({plan['seconds']} s)", flush=True)
            if best is None or rich(plan) > rich(best):
                best = plan
        if not best:
            continue
        # a richer example from an earlier run stays; it gets the places around it from this run (same trip, same Qloo data)
        try:
            old = json.loads((OUT / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
        if old and not fresh and rich(old) > rich(best):
            used = {s["name"].lower() for d in old["days"] for s in d["stops"]}
            old["nearby"] = {k: [p for p in v if p["name"].lower() not in used] for k, v in best.get("nearby", {}).items()}
            print(f"{key}: the earlier plan is richer ({rich(old):.1f}) — kept, with this run's places around it", flush=True)
            best = old
        best["example"] = {k: v for k, v in dict(ex, id=key).items() if k != "weekday"}
        best.pop("llm_log", None)
        await warm_photos(best)
        (OUT / f"{key}.json").write_text(json.dumps(best, ensure_ascii=False), encoding="utf-8")
        print(f"{key}: kept {best['title']} — {sum(len(d['stops']) for d in best['days'])} stops", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
