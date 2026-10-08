"""Rihla web app: FastAPI backend + static frontend.  Run: uvicorn app.main:app --port 8090"""
import asyncio
import datetime as dt
import json
import logging
import re
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, config
from .config import ROOT

log = logging.getLogger("rihla")
app = FastAPI(title="Rihla — halal-aware travel agent", docs_url="/api/docs")


# The free API keys must never leave the server, not even inside a provider's error message
_SECRETS = [v for v in (config.QLOO_API_KEY, config.LLM_API_KEY, config.FALLBACK_API_KEY) if v]
_KEYLIKE = re.compile(r"\b(nvapi-|gsk_|hack_|sk-)[A-Za-z0-9_\-]{6,}")


def _clean(text: str) -> str:
    for v in _SECRETS:
        text = text.replace(v, "***")
    return _KEYLIKE.sub("***", text)


# Fair use: every plan costs Qloo requests (10,000 a month) and LLM calls, so one visitor cannot use them all up.
# Only plans that were made count: a typo or a failed plan never uses up a visitor's turn.
PER_IP_HOUR = 10
PER_DAY = 300
_by_ip: dict[str, deque] = defaultdict(deque)
_today: deque = deque()


def _ip(request: Request) -> str:
    # Vercel sets x-vercel-forwarded-for / x-real-ip itself; x-forwarded-for can carry what the client sent
    h = request.headers
    ip = h.get("x-vercel-forwarded-for") or h.get("x-real-ip") or h.get("x-forwarded-for") or (request.client.host if request.client else "?")
    return ip.split(",")[0].strip()


def _refund(request: Request) -> None:
    """Give the visitor their turn back: the plan could not be made."""
    for q in (_by_ip[_ip(request)], _today):
        if q:
            q.pop()


def _allow(request: Request) -> None:
    now = time.time()
    mine = _by_ip[_ip(request)]
    while mine and now - mine[0] > 3600:
        mine.popleft()
    while _today and now - _today[0] > 86400:
        _today.popleft()
    if len(mine) >= PER_IP_HOUR:
        wait = max(1, round((3600 - (now - mine[0])) / 60))
        raise HTTPException(429, f"You have planned {PER_IP_HOUR} trips in the last hour — you can plan the next one in about "
                                 f"{wait} minute{'s' if wait > 1 else ''}. The example plans and your saved trips still open.")
    if len(_today) >= PER_DAY:
        raise HTTPException(429, "Rihla has planned many trips today — please come back tomorrow.")
    mine.append(now)
    _today.append(now)


class PlanRequest(BaseModel):
    destination: str = Field(min_length=2, max_length=120)
    days: int = Field(default=2, ge=1, le=4)
    start: dt.date | None = None
    travellers: str = Field(default="", max_length=300)
    tastes: str = Field(default="", max_length=600)
    language: str = Field(default="English", max_length=20)


@app.post("/api/plan")
async def make_plan(req: PlanRequest, request: Request):
    _allow(request)
    try:
        return await agent.plan(req.destination, req.days, req.start or dt.date.today() + dt.timedelta(days=7),
                                req.travellers, req.tastes, req.language)
    except ValueError as e:
        _refund(request)
        raise HTTPException(400, str(e))
    except Exception as e:
        _refund(request)
        log.exception("plan failed")
        raise HTTPException(502, _clean(f"Planning failed: {str(e)[:200]}"))


@app.post("/api/plan/stream")
async def make_plan_stream(req: PlanRequest, request: Request):
    """Same as /api/plan, but streams what the agent is doing (server-sent events) and ends with the plan."""
    _allow(request)
    q: asyncio.Queue = asyncio.Queue()

    async def run():
        try:
            plan = await agent.plan(req.destination, req.days, req.start or dt.date.today() + dt.timedelta(days=7),
                                    req.travellers, req.tastes, req.language, emit=q.put)
            await q.put({"type": "done", "plan": plan})
        except ValueError as e:
            _refund(request)
            await q.put({"type": "error", "detail": _clean(str(e))})
        except asyncio.CancelledError:
            _refund(request)  # the visitor left before the plan was ready
            raise
        except Exception as e:
            _refund(request)
            log.exception("plan failed")
            await q.put({"type": "error", "detail": _clean(f"Planning failed: {str(e)[:200]}")})

    async def events():
        task = asyncio.create_task(run())
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # proxies close silent connections
                    continue
                yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                if ev["type"] in ("done", "error"):
                    break
        finally:
            task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/health")
async def health():
    return {"ok": True, "qloo": "mock" if config.QLOO_MOCK else "live", "model": config.LLM_MODEL}


app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.get("/")
async def index():
    return FileResponse(ROOT / "web" / "index.html")
