"""Rihla web app: FastAPI backend + static frontend.  Run: uvicorn app.main:app --port 8090"""
import asyncio
import datetime as dt
import json
import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, config
from .config import ROOT

log = logging.getLogger("rihla")
app = FastAPI(title="Rihla — halal-aware travel agent", docs_url="/api/docs")


class PlanRequest(BaseModel):
    destination: str = Field(min_length=2, max_length=120)
    days: int = Field(default=2, ge=1, le=4)
    start: dt.date | None = None
    travellers: str = Field(default="", max_length=300)
    tastes: str = Field(default="", max_length=600)
    language: str = Field(default="English", max_length=20)


@app.post("/api/plan")
async def make_plan(req: PlanRequest):
    try:
        return await agent.plan(req.destination, req.days, req.start or dt.date.today() + dt.timedelta(days=7),
                                req.travellers, req.tastes, req.language)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("plan failed")
        raise HTTPException(502, f"Planning failed: {str(e)[:200]}")


@app.post("/api/plan/stream")
async def make_plan_stream(req: PlanRequest):
    """Same as /api/plan, but streams what the agent is doing (server-sent events) and ends with the plan."""
    q: asyncio.Queue = asyncio.Queue()

    async def run():
        try:
            plan = await agent.plan(req.destination, req.days, req.start or dt.date.today() + dt.timedelta(days=7),
                                    req.travellers, req.tastes, req.language, emit=q.put)
            await q.put({"type": "done", "plan": plan})
        except ValueError as e:
            await q.put({"type": "error", "detail": str(e)})
        except Exception as e:
            log.exception("plan failed")
            await q.put({"type": "error", "detail": f"Planning failed: {str(e)[:200]}"})

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
