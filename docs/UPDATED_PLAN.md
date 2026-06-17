Revised FRV Optimization & Response Pipeline

This document updates the original 15-phase plan with practical improvements, ordering tweaks, and operational considerations. The codebase will include lightweight, non-breaking stubs so the pipeline can be extended incrementally.

Key changes (summary):
- Add temporal slicing to hotspot detection (time-of-day / weekday vs weekend).
- Make FRV sufficiency estimation stochastic and iterative, producing confidence intervals.
- Prefer constrained optimization (MIP) for district-level transfers; use GA only when needed.
- Use travel-time metrics (OSRM/Mapbox) for p-median/p-center placement instead of Euclidean.
- Introduce an iterative inner loop: sufficiency → transfer optimization → re-simulation until improvement plateaus.
- Account for operational constraints: shift schedules, unit availability, minimum staffing, maximum transferable FRVs.
- Add validation, sensitivity analysis, and reproducibility artifacts (audit, seed, checkpoints).

Revised Phases (condensed + improvements)

Phase 1 — Data Preparation
- Ingest and combine raw LBS CSVs.
- Extract coordinates (with XML parser), geocode fallback for missing coords, normalize timestamps, deduplicate, and validate schema.
- Produce: cleaned incident table with latitude, longitude, timestamp, district, police_station, callid and an audit file.

Phase 2 — Hotspot Detection (spatio-temporal)
- Grid incidents by district and by temporal slices (e.g., peak hours, non-peak, weekday/weekend).
- Run adaptive HDBSCAN per slice and derive hotspot zones and medoids.
- Produce: hotspots per district per time-slice, hotspot severity metrics.

Phase 3 — Current FRV Inventory
- Load master unit export with availability filters (exclude TEST, apply shift calendars).
- Map each FRV to district and PS; produce current_frv_counts with availability factors.

Phase 4 — Baseline Response Simulation
- Use OSRM/Mapbox table endpoints with batching & caching to compute travel-time-based metrics for medoids.
- Produce baseline metrics: avg, median, 95th, max, coverage% per district and PS.

Phase 5 — Demand Scoring
- Combine incident density, hotspot severity, recency-weighted incidents, and baseline coverage gap into demand scores per district/PS/time-slice.

Phase 6 — FRV Sufficiency Estimation (iterative & stochastic)
- For each district, run multiple simulation seeds adding virtual FRVs and placing them by medoid/p-median on travel-time graphs.
- Record how many FRVs achieve targets with confidence intervals.

Phase 7 — District Transfer Optimization (constrained)
- Given current and required FRVs, solve a constrained MIP (transfer limits, min per-district) to minimize shortfall and transfer cost.

Phase 8 — District Re-simulation
- Re-simulate with transfers applied and assess improvements.

Phase 9 — PS-level Optimization (within-district)
- Repeat sufficiency + constrained optimization for PS allocations using PS-level demand and travel-time medoids.

Phase 10 — PS Re-simulation
- Validate improvements at PS granularity.

Phase 11 — Final Placement (p-median on travel-time graph)
- Solve p-median/p-center using travel-time distances; support capacitated variants and shift-aware placement.

Phase 12 — Final System-wide Simulation & Reporting
- Produce final metrics and before/after comparisons with sensitivity analysis.

Phase 13 — Patrol Zone Generation
- Use hotspot medoids + isochrone buffers (drive-time based) to generate patrol zones.

Phase 14 — Patrol Route Optimization
- Solve VRP/TSP per FRV with real road distances and shift constraints; produce route geometries and OSRM paths.

Phase 15 — Dashboard & Artifacts
- Interactive view with state → district → PS navigation and panels for optimization results, demand scores, transfers, and before/after metrics.

Operational & Implementation Notes
- All heavy/long-running steps run behind flags and produce checkpoints in `checkpoints/`.
- Add deterministic seeds, logging, and small unit tests for each module.
- Implement parallel requests, caching, and rate-limit backoff for OSRM/Mapbox calls.

How this patch integrates
- Adds this plan file and non-breaking optimization stubs in `lbs_predictor/optimization.py`.
- Wires a `run_optimization` flag into `run_pipeline` so existing workflows are unchanged unless explicitly enabled.

Next actionable steps
- Replace stubs with real implementations for demand scoring, sufficiency simulation, and constrained optimizers (OR-Tools / PuLP / CVXPY).
- Add tests and a small demo script that runs a fast end-to-end on sampled data.
