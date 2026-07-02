"""
ais_source.py
Live tanker positions via AISStream.io, scoped to corridor bounding boxes.
Falls back to a synthetic tanker-motion generator when no API key is set
or the socket is unreachable, so the map always has something to show.

Also keeps a rolling breadcrumb trail per vessel (recent position history),
so route_network.py can derive real route shapes from actual vessel motion
instead of drawing a straight line between two anchor points.
"""

import asyncio
import json
import logging
import math
import os
import random
import time
from collections import defaultdict, deque

import websockets

from data_sources import CORRIDORS

log = logging.getLogger("ais_source")

AISSTREAM_KEY = os.getenv("AISSTREAM_API_KEY", "")
AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"

TANKER_SHIP_TYPES = set(range(80, 90))  # AIS ship type codes for tankers

TRACK_MAXLEN = 40  # breadcrumb points kept per vessel

# In-memory live state: {mmsi: {lat, lon, course, speed, corridor, name, updated_at}}
_vessels: dict[str, dict] = {}
_synthetic_fleet: list[dict] = []
_mode = "synthetic"  # flips to "live" once AISStream confirms data

# Breadcrumb trails: {mmsi: deque[(lat, lon), ...]} oldest -> newest
_tracks: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRACK_MAXLEN))


def get_vessels() -> list:
    return list(_vessels.values())


def get_mode() -> str:
    return _mode


def _record_breadcrumb(mmsi: str, lat, lon):
    if lat is None or lon is None:
        return
    _tracks[mmsi].append((lat, lon))


def get_corridor_breadcrumbs(corridor_id: str) -> list[list[float]]:
    """All breadcrumb points from vessels currently assigned to this corridor,
    flattened across vessels. route_network.py turns this into a smoothed path."""
    points = []
    for v in _vessels.values():
        if v.get("corridor") != corridor_id:
            continue
        track = _tracks.get(v["mmsi"])
        if track:
            points.extend([list(p) for p in track])
    return points


# ---------------------------------------------------------------------------
# Live AISStream ingestion
# ---------------------------------------------------------------------------
async def run_ais_stream():
    global _mode
    if not AISSTREAM_KEY:
        log.info("No AISSTREAM_API_KEY set — using synthetic vessel generator only")
        await run_synthetic_generator()
        return

    bboxes = [
        [[c["bbox"][0], c["bbox"][1]], [c["bbox"][2], c["bbox"][3]]] for c in CORRIDORS.values()
    ]

    while True:
        try:
            async with websockets.connect(AISSTREAM_URL, ping_interval=15) as ws:
                sub = {"APIKey": AISSTREAM_KEY, "BoundingBoxes": bboxes,
                       "FilterMessageTypes": ["PositionReport"]}
                await ws.send(json.dumps(sub))
                log.info("AISStream subscribed, waiting for messages...")
                _mode = "live"

                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("MessageType") != "PositionReport":
                        continue
                    report = msg.get("Message", {}).get("PositionReport", {})
                    meta = msg.get("MetaData", {})
                    mmsi = str(report.get("UserID", meta.get("MMSI", "")))
                    if not mmsi:
                        continue

                    lat, lon = report.get("Latitude"), report.get("Longitude")
                    corridor = _corridor_for_point(lat, lon)
                    _vessels[mmsi] = {
                        "mmsi": mmsi,
                        "name": meta.get("ShipName", "").strip() or f"MMSI {mmsi}",
                        "lat": lat,
                        "lon": lon,
                        "course": report.get("Cog", 0),
                        "speed": report.get("Sog", 0),
                        "corridor": corridor,
                        "updated_at": time.time(),
                    }
                    _record_breadcrumb(mmsi, lat, lon)
        except Exception as e:
            log.warning(f"AISStream connection lost, retrying in 10s: {e}")
            _mode = "synthetic"
            await asyncio.sleep(10)


def _corridor_for_point(lat, lon):
    if lat is None or lon is None:
        return None
    for cid, meta in CORRIDORS.items():
        s, w, n, e = meta["bbox"]
        if s <= lat <= n and w <= lon <= e:
            return cid
    return None


# ---------------------------------------------------------------------------
# Synthetic tanker generator — plausible routes through each corridor
# ---------------------------------------------------------------------------
def _seed_synthetic_fleet():
    random.seed(42)
    fleet = []
    for cid, meta in CORRIDORS.items():
        s, w, n, e = meta["bbox"]
        for i in range(6):
            lat = random.uniform(s, n)
            lon = random.uniform(w, e)
            fleet.append({
                "mmsi": f"SYN-{cid}-{i}",
                "name": f"Tanker {cid.title()}-{i+1}",
                "lat": lat,
                "lon": lon,
                "course": random.uniform(0, 360),
                "speed": random.uniform(8, 16),
                "corridor": cid,
                "updated_at": time.time(),
            })
    return fleet


async def run_synthetic_generator():
    global _vessels, _synthetic_fleet
    if not _synthetic_fleet:
        _synthetic_fleet = _seed_synthetic_fleet()

    while True:
        for v in _synthetic_fleet:
            rad = math.radians(v["course"])
            step = v["speed"] * 0.0006
            v["lat"] += step * math.cos(rad)
            v["lon"] += step * math.sin(rad)
            v["course"] = (v["course"] + random.uniform(-8, 8)) % 360
            v["updated_at"] = time.time()

            meta = CORRIDORS[v["corridor"]]
            s, w, n, e = meta["bbox"]
            if not (s <= v["lat"] <= n and w <= v["lon"] <= e):
                v["course"] = (v["course"] + 180) % 360  # bounce back toward corridor

            _vessels[v["mmsi"]] = dict(v)
            _record_breadcrumb(v["mmsi"], v["lat"], v["lon"])

        await asyncio.sleep(4)
