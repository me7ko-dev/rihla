"""Builds the example plans shown on the home page (web/examples/*.json) with the live agent.
The agent writes a different plan each time: each example is made a few times and the richest plan is kept.
Run after changing the agent:  python make_examples.py [id ...]"""
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
    "london-family": dict(destination="London", days=2, travellers="Family of four, kids aged 7 and 11",
                          tastes="We love Pixar movies and the Harry Potter books. The kids are into Minecraft and dinosaurs."),
    "istanbul-couple": dict(destination="Sultanahmet, Istanbul", days=2, travellers="Couple in their 30s",
                            tastes="We love Orhan Pamuk's novels, Sami Yusuf's music, Ottoman history, calligraphy, coffee and sweets."),
    "newyork-father-son": dict(destination="Manhattan, New York", days=2, travellers="Father and son (12)",
                               tastes="We love Marvel movies, Spider-Man, the New York Knicks and science. My son reads Percy Jackson."),
}


def richness(plan: dict) -> float:
    """More personal and famous places, fewer stops the code had to add, listed-halal meals, no long rides."""
    score = 0.0
    for day in plan["days"]:
        for s in day["stops"]:
            if s["kind"] == "sight":
                score += 1 + 2 * bool(s.get("topic") or s.get("requested")) + bool(s.get("because")) + bool(s.get("popular"))
                score -= 1.5 * bool(s.get("added_by")) + 0.5 * (not s.get("description"))
            elif s["kind"] == "meal":
                score += {"verified": 1, "likely": 0.5}.get(s.get("halal_level"), 0) - 0.5 * bool(s.get("alcohol"))
        pts = [(s["lat"], s["lon"]) for s in day["stops"] if s["kind"] != "prayer" and s.get("lat") is not None]
        score -= 0.4 * sum(max(0, distance_m(*pts[i], *pts[i + 1]) - 2000) for i in range(len(pts) - 1)) / 1000
    return score


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    start = dt.date.today() + dt.timedelta(days=14)
    for key in sys.argv[1:] or EXAMPLES:
        ex, best = EXAMPLES[key], None
        for _ in range(TRIES):
            try:
                plan = await agent.plan(ex["destination"], ex["days"], start, ex["travellers"], ex["tastes"])
            except Exception as e:  # a slow model call: try again
                print(f"{key}: failed ({str(e)[:80]})", flush=True)
                continue
            print(f"  {key}: {richness(plan):.1f} — {plan['title']} ({plan['seconds']} s)", flush=True)
            if best is None or richness(plan) > richness(best):
                best = plan
        if not best:
            continue
        best["example"] = dict(ex, id=key)
        best.pop("llm_log", None)
        (OUT / f"{key}.json").write_text(json.dumps(best, ensure_ascii=False), encoding="utf-8")
        print(f"{key}: kept {best['title']} — {sum(len(d['stops']) for d in best['days'])} stops", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
