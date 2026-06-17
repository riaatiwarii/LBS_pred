# Plan: Maximize patrol incident/hotspot coverage + show coverage & optimization uplift

## Goal (from request)
1. Maximize **incident coverage** of patrol routes.
2. Raise the route-length budget to **20 km**.
3. Cover as many **hotspots** as possible, prioritizing the **most important (high-crime) areas**.
4. Expose a **crime coefficient** per area.
5. Show **on the map** what % of incidents we cover.
6. Show **how the optimization helps** (coverage before vs after).

This is a practical project, so the emphasis is on a defensible objective (coverage per km within budget) and clear, honest metrics.

---

## Current behaviour (what the code does today)
- `patrol_routing.py`: hard cap `PATROL_MAX_KM = 15.0`. Per FRV, greedy **nearest** waypoint chosen from a top-5 *priority window* (`_PRIORITY_WINDOW=5`), capped at 200 candidate cells per PS. Waypoint "weight" = number of incidents in a ~100 m grid cell, so weight already encodes local crime intensity.
- Coverage per route = `covered_weight / total_PS_weight` (incident-weighted). Per-PS coverage is written to `patrol_route_stats.csv` but **not surfaced to the frontend**.
- `pipeline.py:145-157`: routes are built from **medoids** (one location per cluster per PS) — they do **not** depend on how many FRVs the optimizer placed at a PS, so today the patrol layer can't show an optimization effect.
- `demand.py`: already computes a per-PS/district `demand_score` (incident density + hotspot severity + response penalty + coverage gap) — a natural **crime coefficient**, but it isn't sent to the map.
- Frontend (`patrol.js`): shows per-route "Coverage: N%" in the FRV popup, but no PS/district/overall coverage and no crime coefficient.

---

## Proposed changes

### 1. Route budget → 20 km (configurable)
- Add `patrol_max_km: float = 20.0` and `patrol_max_min: float = 60.0` to `Settings` (`config.py`).
- `generate_patrol_routes` reads these from settings instead of the hard-coded module constants. (Constants kept as fallback defaults.)

### 2. Better coverage objective (maximize incident weight within budget)
Replace the "nearest within top-5 window" selection with a **prize-collecting ratio greedy**:
- At each step pick the *feasible* candidate maximizing `weight / marginal_cost`, where
  `marginal_cost = dist(current→cand) + dist(cand→base) − dist(current→base)`.
- This maximizes **coverage per km**, so the 20 km budget is spent on the highest-crime reachable cells.
- Don't stop at the first budget-busting candidate — scan all remaining for any that still fit (fills the budget better → higher coverage).
- Raise candidate pool (`_MAX_CANDIDATES` 200 → 400) so dense PS aren't truncated.
- Budget feasibility (route incl. return leg ≤ 20 km) is still strictly enforced; `valid` flag unchanged.

Net effect: same data, same constraints, but measurably higher incident coverage per route. (I'll print before/after avg coverage from the run logs as evidence.)

### 3. Crime coefficient
- **Per waypoint:** `crime_coefficient = weight / max_weight_in_PS` (0–1). Added to `patrol_routes.csv` and to the payload (`crimeCoefficient`).
- **Per PS / district:** reuse `demand.py`'s `demand_score` (0–1) as the area crime coefficient; emit it in `map_data.json` (`psCrimeIndex`, `districtCrimeIndex`).
- Frontend: scale/shade waypoint markers by crime coefficient (bigger + deeper red = higher), and show the PS/district crime index in the patrol panel + popups.

### 4. Show coverage % on the map
- Emit aggregate coverage in the payload:
  - per route (exists), per PS (`psCoveragePct`), per district (`districtCoveragePct`), and an **overall** `patrolCoveragePct`.
- `patrol.js`: 
  - District selected → "Incident coverage: X% of weighted incidents".
  - PS selected → "Coverage: Y%".
  - A small **coverage badge** in the patrol panel ("Covering Z% of incidents overall").
- Color the waypoints/route legend to reflect coverage and crime coefficient (extend the Notation panel).

### 5. How optimization helps (current vs optimized coverage)
The honest way to show uplift: route patrols under **both** deployments and compare.
- Build two per-PS FRV location tables from `redistribution_df`: one using `current_ps` counts, one using `assigned_ps` counts (FRVs seeded at the PS medoid coordinates).
- Run `generate_patrol_routes` for each; compute overall + per-district incident coverage for both.
- Write `patrol_coverage_comparison.csv` and emit to payload: `coverageCurrentPct`, `coverageOptimizedPct`, `coverageDeltaPct` (overall and per district).
- Frontend Deployment View: show **"Coverage: current X% → optimized Y% (▲ +Z%)"** and switch the displayed routes with the Current/Optimized toggle.

> Note: #5 is the largest change and touches `pipeline.py` (building current vs optimized location tables). The optimized routes remain the default `patrol_routes.csv` (so the patrol panel is unchanged unless you toggle).

---

## Files affected
- `lbs_predictor/config.py` — new settings (`patrol_max_km`, `patrol_max_min`, optional crime-weight knobs).
- `lbs_predictor/patrol_routing.py` — ratio-greedy objective, budget from settings, crime_coefficient column.
- `lbs_predictor/pipeline.py` — (only for #5) build current & optimized location tables, run routing twice, write comparison.
- `lbs_predictor/clean_mapping.py` — emit coverage aggregates, crime coefficients, comparison into `map_data.json`.
- `lbs_predictor/demand.py` — expose `demand_score` as crime index in outputs (read by clean_mapping; no algorithm change).
- `lbs_predictor/web/patrol.js`, `index.html`, `style.css` — coverage badges, crime-coefficient styling, Notation legend.

## Validation
- I can't run the full pipeline here (`data/` isn't in this environment), so backend changes will be validated with a **small synthetic unit test** (construct tiny incident + FRV DataFrames, assert: every route ≤ 20 km, ratio-greedy coverage ≥ old greedy coverage, crime_coefficient in [0,1]).
- Frontend validated in-browser against a local synthetic `map_data.json` (as before): coverage badges render, waypoint shading by crime coefficient, current→optimized uplift shows.
- You run `python -m lbs_predictor.cli run --optimize` on real data to confirm end-to-end.

## Suggested rollout (so you get value fast)
- **PR A (core):** #1 budget=20, #2 max-coverage objective, #3 crime coefficient (per-waypoint), #4 coverage % on map. Self-contained, low risk, no `pipeline.py` restructure.
- **PR B (uplift):** #5 current-vs-optimized coverage comparison (touches `pipeline.py`). Bigger/riskier.

I recommend doing PR A first, then PR B.
