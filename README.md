# Geopolitical Risk Intelligence Agent

## Run
pip install -r requirements.txt --break-system-packages
python3 main.py

Open http://localhost:8000 — the custom dashboard (not raw Swagger).
Swagger/OpenAPI docs are still available at http://localhost:8000/api/docs

## Optional env vars (falls back to synthetic data if unset — demo never breaks)
- EIA_API_KEY — from eia.gov/opendata/register.php
- AISSTREAM_API_KEY — from aisstream.io/authenticate (sign in via GitHub)

## Files
- data_sources.py — GDELT, IMF PortWatch, OFAC, EIA fetchers, each with synthetic fallback
- risk_engine.py — fuses sub-scores into a composite risk score per corridor, tracks tier/trend/alerts
- ais_source.py — live vessel tracking via AISStream.io, or a synthetic tanker generator if no key
- route_network.py — builds map routes (origin -> chokepoint -> India port) joined with risk + live flow
- main.py — FastAPI app: background 60s refresh loop, serves the dashboard at "/"
- dashboard/index.html — dark command-center map UI (Leaflet + CartoDB dark tiles), polls every 8s

## API endpoints
- GET /api/health
- GET /api/corridors — composite score, tier, trend, sub-scores per corridor
- GET /api/corridors/{id}/history
- GET /api/network/live — routes + live vessels + alerts (what the map consumes)
- GET /api/alerts
- POST /api/refresh — force an immediate re-fetch of all sources

## Notes
- Tested and confirmed running end-to-end with synthetic fallback (this sandbox's network
  doesn't allow GDELT/OFAC/ArcGIS domains, so live fetches 403 and fall back automatically —
  that's the fallback working as designed, not a bug).
- On your own machine with open internet, GDELT/PortWatch/OFAC will pull live data immediately.
  AIS and EIA need the free keys above to go live; otherwise synthetic.
