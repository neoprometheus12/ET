"""
route_network.py
Builds the map's route network: one line per corridor connecting a nominal
Gulf/source anchor to an India/destination anchor, colored by risk and
weighted by live vessel flow density.

Path shape has two layers:
1. A hand-placed waypoint SKELETON per corridor that follows realistic
   maritime routing (hugging coastlines, avoiding landmasses) — this is
   what makes the line actually look like a shipping route instead of a
   ruler-straight cut across Saudi Arabia.
2. Live AIS breadcrumb detail is spliced into the segment that passes
   through the actual strait/chokepoint, replacing the skeleton's single
   placeholder point there with a smoothed multi-point path built from
   real (or synthetic) vessel positions. Everywhere else, AIS coverage
   doesn't exist (real AIS is coastal-only, ~200km range), so the skeleton
   carries the rest of the route — that split mirrors reality rather than
   faking full-route AIS coverage.
"""

from data_sources import CORRIDORS
import ais_source
import risk_engine

# Waypoint skeleton per corridor, ordered origin -> destination.
# Index 1 in every list is the "corridor local" placeholder — the point
# nearest the actual strait/canal — and gets replaced by AIS-derived detail
# when enough breadcrumb data is available. Labels are only set on the
# first (origin) and last (destination) points.
ROUTE_SKELETON = {
    "hormuz": [
        [26.7, 51.5, "Persian Gulf Loading Zone"],
        [26.5, 56.3, None],   # Strait of Hormuz — AIS-derived when available
        [25.4, 57.0, None],   # Gulf of Oman
        [23.6, 59.0, None],   # past Oman coast
        [22.0, 63.0, None],   # mid Arabian Sea
        [21.5, 66.0, None],   # approaching India
        [22.3, 69.1, "Kandla / Gujarat, India"],
    ],
    "bab_el_mandeb": [
        [13.0, 43.2, "Red Sea Approach"],
        [12.6, 43.4, None],   # Bab-el-Mandeb — AIS-derived when available
        [12.0, 45.0, None],   # Gulf of Aden entrance
        [12.5, 49.0, None],   # Gulf of Aden, off Yemen coast
        [14.0, 53.0, None],   # Arabian Sea, past Socotra
        [16.5, 60.0, None],   # Arabian Sea crossing
        [18.0, 66.0, None],   # approaching India
        [18.9, 72.8, "Mumbai / JNPT, India"],
    ],
    "malacca": [
        [1.3, 103.8, "Singapore Strait"],
        [3.0, 101.5, None],   # Strait of Malacca — AIS-derived when available
        [5.5, 98.3, None],    # Northern Malacca / Andaman Sea entrance
        [7.0, 94.0, None],    # Andaman Sea
        [8.5, 90.0, None],    # Bay of Bengal
        [10.5, 85.0, None],   # Bay of Bengal crossing
        [13.1, 80.3, "Chennai / Ennore, India"],
    ],
    "suez": [
        [31.2, 32.3, "Suez Mediterranean Approach"],
        [30.5, 32.5, None],   # Suez Canal — AIS-derived when available
        [28.5, 33.5, None],   # Gulf of Suez
        [24.0, 36.5, None],   # Red Sea, mid
        [18.0, 39.5, None],   # Red Sea, south
        [13.5, 42.5, None],   # approaching Bab-el-Mandeb
        [12.2, 44.0, None],   # Bab-el-Mandeb exit
        [11.0, 50.0, None],   # Gulf of Aden
        [9.0, 60.0, None],    # Arabian Sea
        [8.0, 70.0, None],    # approaching India
        [8.5, 76.9, "Kochi, India"],
    ],
}

CORRIDOR_LOCAL_INDEX = 1  # position of the strait placeholder in every skeleton above

# Bucketing for the AIS-derived local splice (kept small since it only
# covers the short strait segment, not the whole route).
LOCAL_PATH_BUCKETS = 5
MIN_DERIVED_POINTS = 2


def _project_and_smooth(start_xy, end_xy, points, num_buckets):
    """Projects each (lat, lon) breadcrumb onto the start->end line, bucket-averages
    by progress along that line, and returns ordered waypoints start-side to end-side."""
    sx, sy = start_xy
    dx, dy = end_xy[0] - sx, end_xy[1] - sy
    length_sq = dx * dx + dy * dy
    if length_sq == 0 or not points:
        return []

    buckets = [[] for _ in range(num_buckets)]
    for lat, lon in points:
        t = ((lat - sx) * dx + (lon - sy) * dy) / length_sq
        t = max(0.0, min(1.0, t))  # clamp stray points onto the 0..1 range
        idx = min(num_buckets - 1, int(t * num_buckets))
        buckets[idx].append((lat, lon))

    smoothed = []
    for bucket in buckets:
        if not bucket:
            continue
        avg_lat = sum(p[0] for p in bucket) / len(bucket)
        avg_lon = sum(p[1] for p in bucket) / len(bucket)
        smoothed.append([round(avg_lat, 4), round(avg_lon, 4)])

    return smoothed


def _build_path(cid: str) -> tuple[list, str]:
    """Returns (path, path_source) for a corridor: the skeleton with AIS-derived
    detail spliced in at the strait segment if enough breadcrumb data exists."""
    skeleton = ROUTE_SKELETON[cid]
    local_idx = CORRIDOR_LOCAL_INDEX
    prev_point = skeleton[local_idx - 1]
    next_point = skeleton[local_idx + 1]

    breadcrumbs = ais_source.get_corridor_breadcrumbs(cid)
    smoothed = _project_and_smooth(
        (prev_point[0], prev_point[1]),
        (next_point[0], next_point[1]),
        breadcrumbs,
        LOCAL_PATH_BUCKETS,
    )

    if len(smoothed) >= MIN_DERIVED_POINTS:
        local_segment = smoothed
        path_source = "ais_derived"
    else:
        local_segment = [[skeleton[local_idx][0], skeleton[local_idx][1]]]
        path_source = "nominal"

    path = (
        [[p[0], p[1]] for p in skeleton[:local_idx]]
        + local_segment
        + [[p[0], p[1]] for p in skeleton[local_idx + 1:]]
    )
    return path, path_source


def build_network() -> dict:
    risk_by_corridor = risk_engine.get_latest()
    vessels = ais_source.get_vessels()

    vessel_counts = {c: 0 for c in CORRIDORS}
    for v in vessels:
        if v.get("corridor") in vessel_counts:
            vessel_counts[v["corridor"]] += 1

    max_count = max(vessel_counts.values()) or 1

    routes = []
    for cid, meta in CORRIDORS.items():
        skeleton = ROUTE_SKELETON[cid]
        origin_label = skeleton[0][2]
        destination_label = skeleton[-1][2]

        risk = risk_by_corridor.get(cid, {})
        flow_count = vessel_counts[cid]
        flow_norm = round(flow_count / max_count, 2)

        path, path_source = _build_path(cid)

        routes.append({
            "corridor": cid,
            "name": meta["name"],
            "path": path,
            "path_source": path_source,
            "origin_label": origin_label,
            "destination_label": destination_label,
            "risk_score": risk.get("composite", 0),
            "risk_tier": risk.get("tier", "low"),
            "risk_trend": risk.get("trend", "flat"),
            "sub_scores": risk.get("sub_scores", {}),
            "vessel_count": flow_count,
            "flow_intensity": flow_norm,
        })

    return {
        "routes": routes,
        "vessels": vessels,
        "ais_mode": ais_source.get_mode(),
        "alerts": risk_engine.get_alerts(),
    }
