"""Builds the example plans shown on the home page (web/examples/*.json) with the live agent.
Run after changing the agent:  python make_examples.py [id ...]"""
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

from app import agent

OUT = Path(__file__).resolve().parent / "web" / "examples"
EXAMPLES = {
    "london-family": dict(destination="London", days=2, travellers="Family of four, kids aged 7 and 11",
                          tastes="We love Pixar movies and the Harry Potter books. The kids are into Minecraft and dinosaurs."),
    "istanbul-couple": dict(destination="Sultanahmet, Istanbul", days=2, travellers="Couple in their 30s",
                            tastes="We love Orhan Pamuk's novels, Sami Yusuf's music, Ottoman history, calligraphy, coffee and sweets."),
    "newyork-father-son": dict(destination="Manhattan, New York", days=2, travellers="Father and son (12)",
                               tastes="We love Marvel movies, Spider-Man, the New York Knicks and science. My son reads Percy Jackson."),
}


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    start = dt.date.today() + dt.timedelta(days=14)
    for key in sys.argv[1:] or EXAMPLES:
        ex = EXAMPLES[key]
        plan = await agent.plan(ex["destination"], ex["days"], start, ex["travellers"], ex["tastes"])
        plan["example"] = dict(ex, id=key)
        plan.pop("llm_log", None)
        (OUT / f"{key}.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        stops = sum(len(d["stops"]) for d in plan["days"])
        print(f"{key}: {plan['title']} — {stops} stops, {plan['seconds']} s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
