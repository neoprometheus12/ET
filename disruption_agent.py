"""
disruption_agent.py
LLM-based multi-source disruption-probability agent, powered by Gemini.

This is the piece the challenge brief actually asks for and that nothing
else in this codebase does: it reads the SAME underlying signals that
risk_engine.py already fuses (GDELT news-tone, PortWatch transit volume,
OFAC sanctions activity, EIA price volatility) PLUS live AIS vessel/route
context, and asks Gemini to reason across them JOINTLY per corridor —
not just re-average them — producing a disruption probability, a
confidence level, and named drivers.

Design choices:
- Runs on its own slower cadence (called from a separate APScheduler job,
  e.g. every 5-10 min), NOT the 60s loop. LLM calls are slower/costlier
  than arithmetic, so the fast rules-based risk_engine.py stays as the
  always-on baseline; this agent is a periodic overlay, not a replacement.
- Degrades gracefully: no GEMINI_API_KEY, or a failed call, means the
  dashboard keeps the last successful agent result (or a clearly-flagged
  "agent offline" state) and falls back to the rules-based score — same
  fallback philosophy as ais_source.py / data_sources.py elsewhere in
  this project. The demo never breaks.
- Forces structured JSON output via response_schema so the result can be
  rendered on the dashboard as data, not parsed out of free text.
"""

import json
import logging
import os
import time
from typing import Optional

from google import genai
from google.genai import types

from data_sources import CORRIDORS

log = logging.getLogger("disruption_agent")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

_client: Optional[genai.Client] = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

_last_result: dict = {
    "agent_live": False,
    "generated_at": None,
    "model": GEMINI_MODEL,
    "overall_summary": "",
    "corridors": {},
    "note": "Agent has not run yet.",
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "corridors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "corridor_id": {"type": "string"},
                    "disruption_probability": {
                        "type": "number",
                        "description": "0-100 probability of a material supply disruption in this "
                                       "corridor over roughly the next two weeks.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "0-100 confidence in this estimate, given signal quality/freshness.",
                    },
                    "key_drivers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-4 short phrases citing the specific signals behind this estimate.",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "1-2 sentence plain-language rationale.",
                    },
                },
                "required": ["corridor_id", "disruption_probability", "confidence", "key_drivers", "reasoning"],
            },
        },
        "overall_summary": {
            "type": "string",
            "description": "1-2 sentence summary of the disruption picture across all corridors.",
        },
    },
    "required": ["corridors", "overall_summary"],
}


def _build_signal_bundle(corridor_id: str, risk_record: dict, raw_sources: dict,
                          vessel_count: int, path_source: str) -> dict:
    """Compact, LLM-readable snapshot of everything currently known about one
    corridor: the fused rules-based record from risk_engine.py, plus a few
    raw fields carried over from the last data_sources.fetch_all_sources()
    call, plus live AIS/route context from route_network.py."""
    bundle = {
        "corridor_id": corridor_id,
        "corridor_name": CORRIDORS.get(corridor_id, {}).get("name", corridor_id),
        "rules_based_composite_score": risk_record.get("composite"),
        "rules_based_tier": risk_record.get("tier"),
        "rules_based_trend": risk_record.get("trend"),
        "sub_scores": risk_record.get("sub_scores", {}),
        "vessel_count_tracked": vessel_count,
        # "ais_derived" = real vessel breadcrumb data available for this corridor;
        # "nominal" = sparse/no AIS coverage, route is a static skeleton guess.
        "route_path_source": path_source,
    }
    ofac = raw_sources.get("ofac", {})
    if ofac.get("new_designations") is not None:
        bundle["new_sanctions_designations_globally"] = ofac["new_designations"]
    eia = raw_sources.get("eia", {})
    if eia.get("latest_price") is not None:
        bundle["latest_brent_price_usd"] = eia["latest_price"]
    bundle["portwatch_live"] = raw_sources.get("portwatch", {}).get("live", False)
    bundle["gdelt_live"] = raw_sources.get("gdelt", {}).get("live", False)
    return bundle


def _build_prompt(bundles: list) -> str:
    return (
        "You are a geopolitical risk analyst monitoring India's crude oil import "
        "corridors: Strait of Hormuz, Bab-el-Mandeb/Red Sea, Strait of Malacca, "
        "and the Suez Canal. For each corridor below you're given a rules-based "
        "composite risk score (0-100, a weighted blend of GDELT news-tone signals, "
        "IMF PortWatch transit volumes, OFAC sanctions activity, and EIA Brent price "
        "volatility), plus live AIS vessel-tracking context.\n\n"
        "Do not just restate the composite score. Reason across the signals "
        "JOINTLY. For example: a rising composite score combined with falling "
        "vessel counts and new sanctions designations is a materially stronger "
        "disruption signal than any single input alone. A high score built "
        "entirely from non-live (fallback) sources should lower your confidence, "
        "not your probability estimate. Estimate, for each corridor, the "
        "probability (0-100) of a material supply disruption over roughly the "
        "next two weeks, your confidence (0-100) in that estimate, and 2-4 short, "
        "concrete drivers actually grounded in the data given.\n\n"
        f"Corridor signal data:\n{json.dumps(bundles, indent=2)}"
    )


async def run_agent_cycle(risk_records: dict, raw_sources: dict,
                           vessel_counts: dict, path_sources: dict) -> dict:
    """Call this from a slower scheduler job (e.g. every 5-10 min) — separate
    from the 60s rules-based refresh loop in main.py."""
    global _last_result

    if not _client:
        _last_result = {
            **_last_result,
            "agent_live": False,
            "note": "GEMINI_API_KEY not set — agent disabled, dashboard uses rules-based scores only.",
        }
        return _last_result

    bundles = [
        _build_signal_bundle(
            cid,
            risk_records.get(cid, {}),
            raw_sources,
            vessel_counts.get(cid, 0),
            path_sources.get(cid, "nominal"),
        )
        for cid in CORRIDORS
    ]

    try:
        response = await _client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=_build_prompt(bundles),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                temperature=0.2,
            ),
        )
        parsed = json.loads(response.text)
        corridors_out = {c["corridor_id"]: c for c in parsed.get("corridors", [])}

        _last_result = {
            "agent_live": True,
            "generated_at": time.time(),
            "model": GEMINI_MODEL,
            "overall_summary": parsed.get("overall_summary", ""),
            "corridors": corridors_out,
            "note": None,
        }
        log.info("Disruption agent cycle complete (%d corridors)", len(corridors_out))

    except Exception as e:
        log.warning(f"Gemini agent call failed, keeping last known result: {e}")
        _last_result = {**_last_result, "agent_live": False, "note": f"Last call failed: {e}"}

    return _last_result


def get_latest() -> dict:
    return _last_result
