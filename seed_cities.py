"""Pre-fetches OpenStreetMap data for the demo cities into cache/osm (the public Overpass server is slow).
Copy the files to seed/osm to ship them with the app."""
import asyncio
import sys
import time

from app import osm
from app.agent import AREA_M

CITIES = ["Manhattan, New York", "Brooklyn, New York", "Sultanahmet, Istanbul", "Beyoglu, Istanbul", "London",
          "Paris", "Dubai Marina", "Kuala Lumpur", "Sofia, Bulgaria", "Plovdiv, Bulgaria",
          "Barcelona", "Rome", "Sarajevo", "Berlin"]


async def main() -> None:
    for city in sys.argv[1:] or CITIES:
        t = time.time()
        try:
            g = await osm.geocode(city)
            a = await osm.area(g["lat"], g["lon"], AREA_M)
            print(f"{city}: halal {len(a['halal'])}, mosques {len(a['mosques'])} ({time.time() - t:.0f}s)", flush=True)
        except Exception as e:  # keep going: one slow city must not stop the rest
            print(f"{city}: FAILED {e}", flush=True)
        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
