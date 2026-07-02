"""
main.py
Geopolitical Risk Intelligence Agent — API + live dashboard.

Root ("/") serves the custom map dashboard, NOT the raw Swagger page.
Swagger/OpenAPI docs are still available at /api/docs for reference.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import data_sources
import risk_engine
import route_network
import ais_source
import scenario_modeler
import disruption_agent

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

# Resolve paths relative to this file, not the current working directory,
# so it works no matter which folder you run "python main.py" from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_DIR = os.path.join(BASE_DIR, "dashboard")

scheduler = AsyncIOScheduler()
_last_refresh = {"timestamp": None, "status": "starting"}
_last_raw_sources = {}  # last data_sources.fetch_all_sources() output, fed to disruption_agent


async def refresh_risk_scores():
    global _last_raw_sources
    try:
        results = await data_sources.fetch_all_sources()
        _last_raw_sources = results
        risk_engine.fuse(results)
        _last_refresh["timestamp"] = time.time()
        _last_refresh["status"] = "ok"
        log.info("Risk scores refreshed")
    except Exception as e:
        _last_refresh["status"] = f"error: {e}"
        log.error(f"Refresh failed: {e}")


async def run_disruption_agent():
    """Slower-cadence LLM agent cycle — reasons across the same signals
    risk_engine.py fuses, plus live AIS/route context. Runs independently
    of the 60s rules-based refresh loop."""
    try:
        network = route_network.build_network()
        vessel_counts = {r["corridor"]: r["vessel_count"] for r in network["routes"]}
        path_sources = {r["corridor"]: r["path_source"] for r in network["routes"]}
        await disruption_agent.run_agent_cycle(
            risk_engine.get_latest(), _last_raw_sources, vessel_counts, path_sources
        )
        log.info("Disruption agent cycle refreshed")
    except Exception as e:
        log.error(f"Disruption agent cycle failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await refresh_risk_scores()  # populate immediately so dashboard isn't empty
    await run_disruption_agent()  # populate agent state on startup too (no-op if no GEMINI_API_KEY)
    scheduler.add_job(refresh_risk_scores, "interval", seconds=60, id="risk_refresh")
    scheduler.add_job(run_disruption_agent, "interval", minutes=7, id="disruption_agent")
    scheduler.start()
    ais_task = asyncio.create_task(ais_source.run_ais_stream())
    log.info("Background scheduler + AIS stream started")
    yield
    scheduler.shutdown(wait=False)
    ais_task.cancel()


app = FastAPI(
    title="Geopolitical Risk Intelligence Agent",
    description="Real-time disruption risk scoring for India's crude oil chokepoints.",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Dashboard (root interface)
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(os.path.join(DASHBOARD_DIR, "index.html"))


# ---------------------------------------------------------------------------
# API — risk intelligence
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {
        "status": "running",
        "last_refresh": _last_refresh,
        "ais_mode": ais_source.get_mode(),
        "agent_live": disruption_agent.get_latest().get("agent_live", False),
        "corridors": list(data_sources.CORRIDORS.keys()),
    }


@app.get("/api/corridors")
async def corridors():
    """Composite risk score + tier + trend + sub-scores, per corridor."""
    return risk_engine.get_latest()


@app.get("/api/corridors/{corridor_id}/history")
async def corridor_history(corridor_id: str):
    return risk_engine.get_history(corridor_id)


@app.get("/api/network/live")
async def network_live():
    """Everything the map needs in one call: routes, vessels, alerts."""
    return route_network.build_network()


@app.get("/api/alerts")
async def alerts():
    return risk_engine.get_alerts()


@app.post("/api/refresh")
async def force_refresh():
    await refresh_risk_scores()
    return {"status": "refreshed", "timestamp": _last_refresh["timestamp"]}


# ---------------------------------------------------------------------------
# API — multi-source disruption agent (Gemini)
# ---------------------------------------------------------------------------
@app.get("/api/agent/latest")
async def agent_latest():
    """Latest LLM-reasoned disruption probability per corridor. Runs on its
    own ~7min cadence (see run_disruption_agent); agent_live=False means
    GEMINI_API_KEY isn't set or the last call failed — the dashboard should
    fall back to /api/corridors (rules-based) in that case."""
    return disruption_agent.get_latest()


@app.post("/api/agent/refresh")
async def force_agent_refresh():
    await run_disruption_agent()
    return disruption_agent.get_latest()


# ---------------------------------------------------------------------------
# API — disruption scenario modeller
# ---------------------------------------------------------------------------
class ScenarioOverrides(BaseModel):
    capacity_reduction_pct: float | None = None
    duration_days: int | None = None
    cut_bpd: float | None = None
    price_shock_usd_per_bbl: float | None = None
    substitution_capacity: float | None = None
    with_intelligence_layer: bool = True


@app.get("/api/scenarios")
async def scenarios():
    """Metadata for every predefined scenario — powers a frontend picker."""
    return scenario_modeler.list_scenarios()


@app.post("/api/scenarios/{scenario_id}/simulate")
async def simulate(scenario_id: str, overrides: ScenarioOverrides = ScenarioOverrides()):
    """Run the cascading impact model for one scenario, with optional
    severity/duration overrides from the caller."""
    try:
        override_dict = {
            k: v for k, v in overrides.dict(exclude={"with_intelligence_layer"}).items()
            if v is not None
        }
        result = scenario_modeler.simulate_scenario(
            scenario_id,
            overrides=override_dict,
            with_intelligence_layer=overrides.with_intelligence_layer,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
