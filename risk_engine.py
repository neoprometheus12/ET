"""
risk_engine.py
Fuses GDELT / PortWatch / OFAC / EIA sub-scores into one composite
disruption-risk score per corridor, with trend tracking and tiering.
"""

import logging
import time
from collections import deque

from data_sources import CORRIDORS

log = logging.getLogger("risk_engine")

WEIGHTS = {
    "gdelt": 0.35,
    "portwatch": 0.30,
    "ofac": 0.15,
    "eia": 0.20,
}

HISTORY_MAXLEN = 96  # ~24h of 15-min samples

_history: dict[str, deque] = {c: deque(maxlen=HISTORY_MAXLEN) for c in CORRIDORS}
_latest: dict = {}
_alerts: deque = deque(maxlen=50)


def _tier(score: float) -> str:
    if score >= 75:
        return "critical"
    if score >= 55:
        return "high"
    if score >= 30:
        return "elevated"
    return "low"


def _trend(corridor: str, current: float) -> str:
    hist = _history[corridor]
    if len(hist) < 2:
        return "flat"
    prior = hist[-2]["composite"]
    delta = current - prior
    if delta > 3:
        return "rising"
    if delta < -3:
        return "falling"
    return "flat"


def fuse(source_results: dict) -> dict:
    """source_results: {'gdelt': {...,'scores':{corridor:val}}, 'portwatch': {...}, ...}"""
    timestamp = time.time()
    composite_out = {}

    for corridor, meta in CORRIDORS.items():
        weighted_sum = 0.0
        weight_used = 0.0
        sub_scores = {}
        freshness = {}

        for source, weight in WEIGHTS.items():
            result = source_results.get(source)
            if not result:
                continue
            val = result.get("scores", {}).get(corridor)
            if val is None:
                continue
            sub_scores[source] = val
            freshness[source] = {"live": result.get("live", False), "fetched_at": result.get("fetched_at")}
            weighted_sum += val * weight
            weight_used += weight

        composite = round(weighted_sum / weight_used, 1) if weight_used > 0 else 0.0
        tier = _tier(composite)
        trend = _trend(corridor, composite)

        record = {
            "corridor": corridor,
            "name": meta["name"],
            "composite": composite,
            "tier": tier,
            "trend": trend,
            "sub_scores": sub_scores,
            "freshness": freshness,
            "center": meta["center"],
            "bbox": meta["bbox"],
            "timestamp": timestamp,
        }

        prev_tier = _latest.get(corridor, {}).get("tier")
        if prev_tier and prev_tier != tier and tier in ("high", "critical"):
            _alerts.appendleft({
                "corridor": corridor,
                "name": meta["name"],
                "message": f"{meta['name']} risk moved {prev_tier} -> {tier} (score {composite})",
                "timestamp": timestamp,
            })

        _history[corridor].append(record)
        composite_out[corridor] = record

    _latest.clear()
    _latest.update(composite_out)
    return composite_out


def get_latest() -> dict:
    return _latest


def get_history(corridor: str) -> list:
    return list(_history.get(corridor, []))


def get_alerts() -> list:
    return list(_alerts)
