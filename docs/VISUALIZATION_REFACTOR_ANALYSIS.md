# LBS Predictor — Visualization Layer Analysis & Proposed Fix

## 1. Active vs obsolete files

### Backend (Python) — ACTIVE
`cli.py`, `pipeline.py`, `config.py`, `ingestion.py`, `cleaning.py`, `geo.py`,
`clustering.py`, `demand.py`, `frv.py`, `response_time.py`, `patrol_routing.py`,
`optimization_refactored.py`, `clean_mapping.py`.

### Backend (Python) — OBSOLETE (imported nowhere)
- `mapping.py`  → superseded by `clean_mapping.py`
- `optimization.py` → superseded by `optimization_refactored.py`

### Frontend (`lbs_predictor/web/`)
- `index.html`, `map.js`, `style.css`, `assets/*` — ACTIVE
- `map.js` — already migrated to `map_data.json` (good)
- `patrol.js` — **HALF-MIGRATED**: still boots from `patrol_data.json`
- `patrol_data.json` — OBSOLETE input (not produced by current backend)

## 2. Execution flow

```
cli.py run --optimize
  └─ pipeline.run_pipeline()
       ingest → clean → assign_boundaries → run_district_level_clustering
       → run_full_optimization_refactored  (transfer_df, redistribution_df)
       → generate_patrol_routes()          → data/outputs/patrol_routes.csv
       → compute_demand_scores()
       → generate_map()  ── clean_mapping.generate_clean_map()
```

## 3. How optimization outputs reach the frontend

`clean_mapping.generate_clean_map()` reads the optimization CSV/JSON outputs +
`patrol_routes.csv`, builds ONE payload and writes it to **two** places:
- `data/outputs/map_data.json`
- `lbs_predictor/web/map_data.json`   ← the file the browser loads

Payload keys (single source of truth):
`districtGeojson, psGeojson, districtBounds, psBounds, districtPsMap,
frvPoints, currentFrvPoints, optimizedFrvPoints, transferRows,
resimulationRows, patrolRoutes, psPoints, allBounds, defaultCenter, defaultZoom`

`patrolRoutes[i]` = `{ routeId, frvId, district, ps, points[{lat,lon,stopSeq,stopType,...}],
routeLengthKm, durationMin, coverage, waypointCount, valid }`

## 4. Root cause of the broken visualization

`map.js` consumes `map_data.json` correctly and already exposes
`window._lbsMapPayload`, `window._lbsMap`, `window._lbsDeploymentMode`.

`patrol.js` is the problem:
1. `initPatrol()` does `fetch("patrol_data.json")` → **404** (file not produced by backend).
   On failure it replaces the whole Patrol panel with an error and `return`s, so the
   entire patrol UI is dead.
2. All patrol-panel population (`populatePatrolDistricts`, `onPatrolDistrict`,
   `onPatrolPs`) reads `patrol.data.routeData[scenario][district][ps]` /
   `patrol.data.distStats` / `patrol.data.psBounds` — none of which exist anymore.
3. Legacy scenario selector (`1200 / 10m / 5m / actual`) belongs to the old
   per-scenario architecture.
4. **The backend-driven code already exists** but is unreachable: `getPatrolFrvsForPs`,
   `getBackendPatrolRoutesForAssignments`, `getBackendPatrolRoute`, `routeToRouteInfo`,
   `buildBackendRoutePopup` already read `window._lbsMapPayload.patrolRoutes`. They are
   gated behind the dead `patrol_data.json` boot path.
5. Minor dead bug: `togglePatrolAnimation()` references an undefined `btn` (currently
   unwired, so not thrown — flagged as dead-code candidate, not fixed yet).

## 5. Proposed architecture (visualization-only)

Make `map_data.json` the single source of truth for patrol too.

`patrol.js` new flow:
- Boot from `window._lbsMapPayload` (set by map.js) instead of `fetch(patrol_data.json)`.
- Populate `#patrol-district` from districts present in `payload.patrolRoutes`
  (fallback `payload.districtPsMap`).
- On district: draw PS boundaries from `payload.psGeojson`/`psBounds`, populate
  `#patrol-ps` from PS values present in `payload.patrolRoutes` for that district.
- On PS: render **backend** routes from `payload.patrolRoutes` (polyline from
  `route.points`, waypoint markers where `stopType==="WAYPOINT"`, route popups via
  existing `buildBackendRoutePopup`), show metrics (length/duration/coverage/waypoints).
- Animation: animate the backend route polyline (`route.points`) — no JS route building.
- Keep `window._showFrv` working off backend routes for FRV marker clicks.

### Legacy handling (per rules — don't delete yet)
- The `#patrol-scenario` selector becomes irrelevant. Phase 1: neutralize it (hide /
  ignore its value) so the new flow works. Phase 2 (separate): remove the selector
  markup + the JS functions that only consumed `patrol.data` (`buildFrvPatrolAssignments`,
  `collectWaypointRefs`, `expandPatrolFrvs`, `buildBudgetedPatrolRoute`,
  `estimateRouteLength`, etc.) as dead-code candidates.

## 6. Benefits
- Eliminates the `patrol_data.json` 404 and the dead patrol panel.
- One backend payload drives the whole map (current + optimized + transfers + patrol).
- No route generation in JS — routes match backend `patrol_routes.csv` exactly.
- Removes ~400 lines of JS scenario/route-building logic (phase 2).

## 7. Migration effort
- Rewrite ~5 functions in `patrol.js` (boot + 3 populate/handlers + animation source).
- Neutralize the scenario `<select>` in `index.html`.
- No backend changes. Low risk, isolated to the web layer.

## 8. Testing constraint (needs a decision)
`data/` is gitignored and absent, so the pipeline can't be run here to regenerate
`map_data.json`. The root `map_data_local.json` (45 MB) is an OLD-schema payload — it has
`frvPoints`/`psPoints`/geojson but **no** `currentFrvPoints / optimizedFrvPoints /
transferRows / patrolRoutes`. So I can validate map.js layers, but to validate the
Patrol UI I'll build a small **local-only** synthetic `map_data.json` (real PS/geojson
data + a few fabricated patrol routes/transfers) purely for browser testing — not committed.
</content>
</invoke>
