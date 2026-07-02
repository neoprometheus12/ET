"""
data_sources.py
Ingestion layer: GDELT, IMF PortWatch, OFAC/UN Sanctions, EIA Brent prices.
Every fetcher degrades to synthetic data on failure so the demo never breaks.
"""

import asyncio
import csv
import io
import logging
import os
import random
import time
import zipfile
from datetime import datetime, timezone

import aiohttp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("data_sources")

# ---------------------------------------------------------------------------
# Corridor definitions — static bounding boxes [south, west, north, east]
# ---------------------------------------------------------------------------
CORRIDORS = {
    "hormuz": {
        "name": "Strait of Hormuz",
        "bbox": [24.5, 55.0, 27.5, 57.5],
        "center": [26.5, 56.3],
    },
    "bab_el_mandeb": {
        "name": "Bab-el-Mandeb / Red Sea",
        "bbox": [11.5, 42.5, 14.0, 44.5],
        "center": [12.6, 43.4],
    },
    "malacca": {
        "name": "Strait of Malacca",
        "bbox": [1.0, 100.0, 6.0, 104.5],
        "center": [3.0, 101.5],
    },
    "suez": {
        "name": "Suez Canal",
        "bbox": [29.5, 32.0, 31.5, 33.0],
        "center": [30.5, 32.5],
    },
}

EIA_API_KEY = os.getenv("EIA_API_KEY", "")
GDELT_LASTUPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
PORTWATCH_URL = (
    "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
    "Chokepoints_Daily_Transit_Calls/FeatureServer/0/query"
    "?where=1%3D1&outFields=*&f=json&orderByFields=date%20DESC&resultRecordCount=200"
)
OFAC_SDN_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV"
EIA_BRENT_URL = (
    f"https://api.eia.gov/v2/petroleum/pri/spt/data/"
    f"?api_key={EIA_API_KEY}&frequency=daily&data[0]=value"
    f"&facets[series][]=RBRTE&sort[0][column]=period&sort[0][direction]=desc&length=10"
)

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=12)


def _corridor_hint(text: str) -> str | None:
    text = (text or "").lower()
    if any(k in text for k in ["hormuz", "iran", "strait of hormuz", "persian gulf"]):
        return "hormuz"
    if any(k in text for k in ["red sea", "bab-el-mandeb", "houthi", "yemen"]):
        return "bab_el_mandeb"
    if any(k in text for k in ["malacca", "singapore strait"]):
        return "malacca"
    if any(k in text for k in ["suez", "egypt canal"]):
        return "suez"
    return None


# ---------------------------------------------------------------------------
# GDELT — global news events, filtered to corridor-relevant conflict signals
# ---------------------------------------------------------------------------
async def fetch_gdelt(session: aiohttp.ClientSession, retries: int = 3) -> dict:
    for attempt in range(1, retries + 1):
        try:
            async with session.get(GDELT_LASTUPDATE_URL, timeout=HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                text = await resp.text()
            lines = [l for l in text.strip().splitlines() if l.strip()]
            events_url = next((l.split(" ")[-1] for l in lines if "export.CSV.zip" in l), None)
            if not events_url:
                raise ValueError("no export CSV found in lastupdate.txt")

            async with session.get(events_url, timeout=HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                raw = await resp.read()

            zf = zipfile.ZipFile(io.BytesIO(raw))
            csv_name = zf.namelist()[0]
            data = zf.read(csv_name).decode("utf-8", errors="ignore")

            scores = {c: [] for c in CORRIDORS}
            for row in csv.reader(io.StringIO(data), delimiter="\t"):
                if len(row) < 35:
                    continue
                try:
                    goldstein = float(row[30])
                    avg_tone = float(row[34])
                    actor1_name = row[6] or ""
                    actor2_name = row[16] or ""
                    action_geo_full = row[51] if len(row) > 51 else ""
                except (ValueError, IndexError):
                    continue

                corridor = _corridor_hint(actor1_name + " " + actor2_name + " " + action_geo_full)
                if corridor:
                    # Goldstein -10..+10 (lower = more destabilizing) -> 0..100 risk
                    risk = max(0.0, min(100.0, (10 - goldstein) * 5 - avg_tone * 0.3))
                    scores[corridor].append(risk)

            out = {}
            for c in CORRIDORS:
                vals = scores[c]
                out[c] = round(sum(vals) / len(vals), 1) if vals else 15.0
            return {"source": "gdelt", "live": True, "scores": out, "fetched_at": _now()}

        except Exception as e:
            log.warning(f"GDELT attempt {attempt}/{retries} failed: {e}")
            await asyncio.sleep(1.5 * attempt)

    return _synthetic_score("gdelt")


# ---------------------------------------------------------------------------
# IMF PortWatch — chokepoint transit call volumes
# ---------------------------------------------------------------------------
async def fetch_portwatch(session: aiohttp.ClientSession) -> dict:
    try:
        async with session.get(PORTWATCH_URL, timeout=HTTP_TIMEOUT) as resp:
            resp.raise_for_status()
            payload = await resp.json()

        features = payload.get("features", [])
        baseline = {c: [] for c in CORRIDORS}
        recent = {c: [] for c in CORRIDORS}

        for f in features:
            attrs = f.get("attributes", {})
            name = (attrs.get("chokepoint") or attrs.get("portname") or "").lower()
            corridor = _corridor_hint(name)
            if not corridor:
                continue
            n_transits = attrs.get("n_transits") or attrs.get("value")
            if n_transits is None:
                continue
            recent[corridor].append(float(n_transits))

        out = {}
        for c in CORRIDORS:
            vals = recent[c]
            if not vals:
                out[c] = 20.0
                continue
            avg_recent = sum(vals[: max(1, len(vals) // 3)]) / max(1, len(vals) // 3)
            avg_baseline = sum(vals) / len(vals)
            drop_pct = 0 if avg_baseline == 0 else max(0, (avg_baseline - avg_recent) / avg_baseline)
            out[c] = round(min(100.0, drop_pct * 150), 1)
        return {"source": "portwatch", "live": True, "scores": out, "fetched_at": _now()}

    except Exception as e:
        log.warning(f"PortWatch fetch failed: {e}")
        return _synthetic_score("portwatch")


# ---------------------------------------------------------------------------
# OFAC SDN List — sanctions designations, snapshot diffing for new entries
# ---------------------------------------------------------------------------
_ofac_last_snapshot: set[str] = set()
OFAC_TIMEOUT = aiohttp.ClientTimeout(total=25)


async def fetch_ofac(session: aiohttp.ClientSession, retries: int = 3) -> dict:
    global _ofac_last_snapshot
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }

    for attempt in range(1, retries + 1):
        try:
            async with session.get(OFAC_SDN_URL, headers=headers, timeout=OFAC_TIMEOUT) as resp:
                resp.raise_for_status()
                text = await resp.text()

            reader = csv.reader(io.StringIO(text))
            entries = {}
            for row in reader:
                if len(row) < 3:
                    continue
                uid, name, program = row[0], row[1], row[2] if len(row) > 2 else ""
                entries[uid] = f"{name} {program}"

            current_ids = set(entries.keys())
            new_ids = current_ids - _ofac_last_snapshot if _ofac_last_snapshot else set()
            _ofac_last_snapshot = current_ids

            hits = {c: 0 for c in CORRIDORS}
            for uid in new_ids:
                corridor = _corridor_hint(entries[uid])
                if corridor:
                    hits[corridor] += 1

            out = {c: round(min(100.0, hits[c] * 25), 1) for c in CORRIDORS}
            return {"source": "ofac", "live": True, "scores": out, "new_designations": len(new_ids), "fetched_at": _now()}

        except asyncio.TimeoutError:
            log.warning(f"OFAC attempt {attempt}/{retries} failed: Timeout after {OFAC_TIMEOUT.total}s")
        except Exception as e:
            reason = str(e) or repr(e)
            log.warning(f"OFAC attempt {attempt}/{retries} failed: {reason}")

        if attempt < retries:
            await asyncio.sleep(1.5 * attempt)

    return _synthetic_score("ofac")


# ---------------------------------------------------------------------------
# EIA — Brent spot price, used as a market-implied volatility signal
# ---------------------------------------------------------------------------
_eia_price_history: list[float] = []


async def fetch_eia_brent(session: aiohttp.ClientSession) -> dict:
    if not EIA_API_KEY:
        return _synthetic_score("eia")
    try:
        async with session.get(EIA_BRENT_URL, timeout=HTTP_TIMEOUT) as resp:
            resp.raise_for_status()
            payload = await resp.json()

        rows = payload.get("response", {}).get("data", [])
        prices = [float(r["value"]) for r in rows if r.get("value") is not None]
        if not prices:
            raise ValueError("no price rows returned")

        _eia_price_history.extend(prices)
        latest = prices[0]
        volatility = (max(prices) - min(prices)) / (sum(prices) / len(prices)) * 100

        # Same market signal applied to every corridor (global price shock)
        risk = round(min(100.0, volatility * 4), 1)
        out = {c: risk for c in CORRIDORS}
        return {"source": "eia", "live": True, "scores": out, "latest_price": latest, "fetched_at": _now()}

    except Exception as e:
        log.warning(f"EIA fetch failed: {e}")
        return _synthetic_score("eia")


# ---------------------------------------------------------------------------
# Synthetic fallback — keeps the demo alive when live sources are unreachable
# ---------------------------------------------------------------------------
_SYNTHETIC_BIAS = {"hormuz": 55, "bab_el_mandeb": 60, "malacca": 25, "suez": 40}


def _synthetic_score(source: str) -> dict:
    out = {}
    for c in CORRIDORS:
        base = _SYNTHETIC_BIAS[c]
        out[c] = round(max(0.0, min(100.0, base + random.uniform(-12, 12))), 1)
    return {"source": source, "live": False, "scores": out, "fetched_at": _now()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Orchestrator — pulls all four sources concurrently
# ---------------------------------------------------------------------------
async def fetch_all_sources() -> dict:
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            fetch_gdelt(session),
            fetch_portwatch(session),
            fetch_ofac(session),
            fetch_eia_brent(session),
            return_exceptions=True,
        )

    clean = []
    for r in results:
        if isinstance(r, Exception):
            log.error(f"source raised: {r}")
            continue
        clean.append(r)
    return {r["source"]: r for r in clean}
