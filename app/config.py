"""Settings read from the environment (.env next to the project)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

# Cache for Qloo and OpenStreetMap responses: the project folder locally, /tmp on Vercel (read-only elsewhere)
CACHE = Path(os.getenv("RIHLA_CACHE") or ("/tmp/rihla-cache" if os.getenv("VERCEL") else ROOT / "cache"))
# OpenStreetMap data for the example cities, shipped with the code (the public Overpass server is slow)
SEED = ROOT / "seed"

QLOO_API_KEY = os.getenv("QLOO_API_KEY", "")
QLOO_BASE_URL = os.getenv("QLOO_BASE_URL", "https://hackathon.api.qloo.com")
# Without a key the Qloo client answers from fixtures/ so the UI can be built before the key arrives
QLOO_MOCK = not QLOO_API_KEY

# Primary LLM: NVIDIA Nemotron (free NIM endpoint, good tool calling); fallback: Groq
LLM_API_KEY = os.getenv("NVIDIA_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b")
FALLBACK_API_KEY = os.getenv("GROQ_API_KEY", "")
FALLBACK_BASE_URL = os.getenv("FALLBACK_BASE_URL", "https://api.groq.com/openai/v1")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "openai/gpt-oss-120b")

# OpenStreetMap services ask for an identifying User-Agent
USER_AGENT = "Rihla/0.1 (halal-aware travel agent; github.com/me7ko-dev/rihla)"
