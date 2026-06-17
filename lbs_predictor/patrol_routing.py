"""
patrol_routing.py
=================
Budget-constrained patrol route generation for LBS Predictor.

Design principles
-----------------
* Hard route budget: every route <= patrol_max_km (default 20 km).
* Maximise incident-weighted coverage, not raw waypoint count.
* Each FRV generates its own route starting and ending at its base.
* The most crime-dense reachable hotspots are visited first, spending
  the budget where it covers the most incidents per km.
* When a waypoint would bust the budget, it is skipped.
  If remaining waypoints still exist, a new route is opened for the
  next available FRV.  Routes are never split mid-FRV; leftover
  waypoints wait for the next FRV's route.
* Output: one row per waypoint visit including start/end base stop.

Algorithm (per FRV) — prize-collecting (orienteering) greedy
-----------------------------------------------------------
Step A  Aggregate PS incidents into weighted waypoint cells.
Step B  Start at FRV base.
Step C  Among all unvisited cells that still fit the budget (route incl.
        return-to-base leg <= patrol_max_km), pick the one with the best
        coverage-per-km ratio:
            marginal = dist(current, cand) + dist(cand, base)
                       - dist(current, base)
            ratio    = weight / marginal
        This maximises incident weight covered for each extra km, so the
        budget is spent on the highest-crime reachable hotspots instead
        of merely the nearest one.
Step D  Repeat until no unvisited cell fits the budget.
Step E  Close the route back to base.

Validation
----------
Routes where length > PATROL_MAX_KM or duration > PATROL_MAX_MIN
are logged as warnings and flagged in the output column `valid`.

Output columns
--------------
frv_id, route_id, stop_seq, stop_type,
latitude, longitude,
cumulative_dist_km, return_dist_km, total_route_km,
est_duration_min, incident_weight, coverage_pct, valid
"""

from __future__ import annotations

import logging
from typing import List, Tuple, Dict

import numpy as np
import pandas as pd

from .config import Settings
from .response_time import aerial_distance_km

logger = logging.getLogger(__name__)

# ── Hard operational limits ──────────────────────────────────────────────────
PATROL_MAX_KM:  float = 20.0   # every route must be ≤ this (fallback; see Settings)
PATROL_MAX_MIN: float = 60.0   # every route duration must be ≤ this (fallback; see Settings)

# Maximum waypoints to build the candidate pool from for a single PS.
# Keeps runtime O(manageable) even for huge urban PS.
_MAX_CANDIDATES: int = 400


# ── Distance helper ──────────────────────────────────────────────────────────

def _dist(lat1: float, lon1: float, lat2: float, lon2: float, settings: Settings) -> float:
    """Aerial distance in km, scaled by road factor."""
    return aerial_distance_km(lat1, lon1, lat2, lon2, settings) * settings.road_factor


# ── Waypoint builder ─────────────────────────────────────────────────────────

def _build_waypoints(
    ps_incidents: pd.DataFrame,
    max_candidates: int = _MAX_CANDIDATES,
) -> List[Dict]:
    """
    Aggregate PS incidents into a ranked waypoint list.
    Returns list of dicts with keys: lat, lon, weight.
    Sorted descending by weight (highest priority first).
    """
    df = ps_incidents.copy()
    df["lat_grid"] = df["latitude"].round(3)
    df["lon_grid"] = df["longitude"].round(3)
    grid = (
        df.groupby(["lat_grid", "lon_grid"])
        .size()
        .reset_index(name="weight")
        .rename(columns={"lat_grid": "lat", "lon_grid": "lon"})
        .sort_values("weight", ascending=False)
        .head(max_candidates)
    )
    return grid.to_dict("records")   # already sorted high → low weight


# ── Single-FRV route builder ─────────────────────────────────────────────────

def _build_one_route(
    base_lat: float,
    base_lon: float,
    waypoints: List[Dict],          # full ranked list, mutated in place
    settings: Settings,
    max_km: float = PATROL_MAX_KM,
) -> Tuple[List[Dict], float]:
    """
    Build one patrol route for a single FRV starting at (base_lat, base_lon).

    Visits as many high-priority waypoints as the budget allows.
    Removes visited waypoints from the `waypoints` list (mutates caller's list).

    Returns (stops, total_route_km).
    stops: list of dicts with lat, lon, weight, stop_type, cumulative_dist_km
    """
    stops: List[Dict] = []
    cumulative = 0.0

    current_lat, current_lon = base_lat, base_lon

    # Track which indices have been visited
    visited: set[int] = set()

    # Base — departure
    stops.append({
        "lat": base_lat, "lon": base_lon,
        "weight": 0, "stop_type": "BASE_START",
        "cumulative_dist_km": 0.0,
    })

    while True:
        # Prize-collecting selection: among all unvisited cells that still fit
        # the budget (route incl. return-to-base leg ≤ max_km), pick the one
        # with the best coverage-per-km ratio (weight / marginal distance).
        leg_back_from_current = _dist(
            current_lat, current_lon, base_lat, base_lon, settings
        )

        best_i = -1
        best_ratio = -1.0
        best_leg = 0.0
        for i in range(len(waypoints)):
            if i in visited:
                continue
            wp = waypoints[i]
            leg_to   = _dist(current_lat, current_lon, wp["lat"], wp["lon"], settings)
            leg_back = _dist(wp["lat"], wp["lon"], base_lat, base_lon, settings)
            if cumulative + leg_to + leg_back > max_km:
                continue  # would bust the budget — infeasible
            marginal = max(leg_to + leg_back - leg_back_from_current, 1e-6)
            ratio = wp["weight"] / marginal
            if ratio > best_ratio:
                best_ratio = ratio
                best_i = i
                best_leg = leg_to

        if best_i < 0:
            break  # nothing else fits the budget; close the route

        wp = waypoints[best_i]
        cumulative += best_leg
        current_lat, current_lon = wp["lat"], wp["lon"]
        visited.add(best_i)

        stops.append({
            "lat": wp["lat"], "lon": wp["lon"],
            "weight": wp["weight"], "stop_type": "WAYPOINT",
            "cumulative_dist_km": round(cumulative, 3),
        })

    # Return leg to base
    return_leg = _dist(current_lat, current_lon, base_lat, base_lon, settings)
    cumulative += return_leg

    stops.append({
        "lat": base_lat, "lon": base_lon,
        "weight": 0, "stop_type": "BASE_END",
        "cumulative_dist_km": round(cumulative, 3),
    })

    # Remove visited waypoints from caller's list (high-to-low index to keep
    # indices stable during deletion)
    for i in sorted(visited, reverse=True):
        waypoints.pop(i)

    return stops, round(cumulative, 3)


# ── Coverage calculator ───────────────────────────────────────────────────────

def _coverage_pct(stops: List[Dict], total_weight: float) -> float:
    covered = sum(s["weight"] for s in stops if s["stop_type"] == "WAYPOINT")
    return round(covered / max(total_weight, 1) * 100, 1)


# ── Main public function ──────────────────────────────────────────────────────

def generate_patrol_routes(
    settings: Settings,
    final_locations: pd.DataFrame,
    incidents: pd.DataFrame,
    max_km: float | None = None,
    max_min: float | None = None,
) -> pd.DataFrame:
    """
    Generate budget-constrained patrol routes.

    Parameters
    ----------
    settings        : project Settings
    final_locations : DataFrame with columns [frv_id (optional), ps, latitude, longitude]
    incidents       : incident DataFrame with columns [ps, latitude, longitude]
    max_km          : hard route length limit; defaults to settings.patrol_max_km (20 km)
    max_min         : hard duration limit in minutes; defaults to settings.patrol_max_min (60 min)

    Returns
    -------
    DataFrame with one row per waypoint stop, columns:
        frv_id, route_id, stop_seq, stop_type,
        latitude, longitude,
        cumulative_dist_km, total_route_km,
        est_duration_min, incident_weight, crime_coefficient,
        coverage_pct, valid
    """
    if max_km is None:
        max_km = getattr(settings, "patrol_max_km", PATROL_MAX_KM)
    if max_min is None:
        max_min = getattr(settings, "patrol_max_min", PATROL_MAX_MIN)

    logger.info("")
    logger.info("=" * 65)
    logger.info("PATROL ROUTE GENERATION")
    logger.info("  Hard limit: %.1f km / %.0f min per route", max_km, max_min)
    logger.info("=" * 65)

    all_rows: List[Dict] = []
    global_route_id = 0
    ps_stats: List[Dict] = []

    # Group FRVs by PS
    ps_groups = final_locations.groupby("ps")

    for ps, frv_group in ps_groups:
        ps_inc = incidents[incidents["ps"] == ps]
        if ps_inc.empty:
            logger.warning("  PS %-25s | no incidents — skipping patrol", ps)
            continue

        # Build ranked waypoints once for this PS
        waypoints = _build_waypoints(ps_inc)
        total_ps_weight = sum(w["weight"] for w in waypoints)
        # Crime coefficient normaliser: the busiest cell in this PS = 1.0
        ps_max_weight = max((w["weight"] for w in waypoints), default=0)

        if not waypoints:
            continue

        frv_list = frv_group.reset_index(drop=True)
        n_frvs = len(frv_list)

        ps_routes_generated = 0
        ps_total_km = 0.0
        ps_covered_weight = 0.0

        # Each FRV gets one route from the remaining waypoint pool
        for frv_idx, frv_row in frv_list.iterrows():
            if not waypoints:
                break  # all waypoints assigned

            frv_id = frv_row.get("frv_id", f"FRV-{ps}-{frv_idx}")
            base_lat = float(frv_row["latitude"])
            base_lon = float(frv_row["longitude"])

            stops, route_km = _build_one_route(
                base_lat, base_lon, waypoints, settings, max_km
            )

            # If only BASE_START + BASE_END (no waypoints reached), skip
            if len(stops) <= 2:
                logger.debug(
                    "  FRV %-20s | no waypoints reachable within %.1f km",
                    frv_id, max_km,
                )
                continue

            est_duration = round(route_km / settings.frv_avg_speed_kph * 60, 1)
            cov_pct = _coverage_pct(stops, total_ps_weight)
            valid = route_km <= max_km and est_duration <= max_min

            if not valid:
                logger.warning(
                    "  ⚠ INVALID ROUTE: FRV %-20s | %.2f km | %.1f min "
                    "(limits: %.1f km / %.0f min)",
                    frv_id, route_km, est_duration, max_km, max_min,
                )

            global_route_id += 1
            ps_routes_generated += 1
            ps_total_km += route_km
            ps_covered_weight += sum(
                s["weight"] for s in stops if s["stop_type"] == "WAYPOINT"
            )

            for seq, stop in enumerate(stops, 1):
                crime_coeff = (
                    round(stop["weight"] / ps_max_weight, 4)
                    if ps_max_weight and stop["stop_type"] == "WAYPOINT"
                    else 0.0
                )
                all_rows.append({
                    "frv_id":              frv_id,
                    "route_id":            global_route_id,
                    "stop_seq":            seq,
                    "stop_type":           stop["stop_type"],
                    "latitude":            stop["lat"],
                    "longitude":           stop["lon"],
                    "cumulative_dist_km":  stop["cumulative_dist_km"],
                    "total_route_km":      round(route_km, 3),
                    "est_duration_min":    est_duration,
                    "incident_weight":     stop["weight"],
                    "crime_coefficient":   crime_coeff,
                    "coverage_pct":        cov_pct,
                    "valid":               valid,
                    "ps":                  ps,
                })

            logger.info(
                "  PS %-22s | FRV %-15s | route_id=%d | "
                "%.2f km | %.1f min | %d waypoints | cov=%.1f%% | valid=%s",
                ps, frv_id, global_route_id,
                route_km, est_duration,
                sum(1 for s in stops if s["stop_type"] == "WAYPOINT"),
                cov_pct, "✓" if valid else "✗",
            )

        ps_stats.append({
            "ps":                ps,
            "n_frvs":            n_frvs,
            "routes_generated":  ps_routes_generated,
            "remaining_waypoints": len(waypoints),
            "total_km":          round(ps_total_km, 2),
            "covered_weight":    ps_covered_weight,
            "total_weight":      total_ps_weight,
            "ps_coverage_pct":   round(ps_covered_weight / max(total_ps_weight, 1) * 100, 1),
        })

    # ── Summary ──────────────────────────────────────────────────────────────
    df = pd.DataFrame(all_rows)

    n_routes = df["route_id"].nunique() if not df.empty else 0
    n_invalid = df[df["valid"] == False]["route_id"].nunique() if not df.empty else 0

    logger.info("")
    logger.info("  Routes generated : %d", n_routes)
    logger.info("  Invalid routes   : %d  (see warnings above)", n_invalid)

    if ps_stats:
        stats_df = pd.DataFrame(ps_stats)
        logger.info(
            "  Avg route km     : %.2f",
            df.drop_duplicates("route_id")["total_route_km"].mean() if not df.empty else 0,
        )
        logger.info(
            "  Avg PS coverage  : %.1f%%",
            stats_df["ps_coverage_pct"].mean(),
        )

    logger.info("=" * 65 + "\n")

    # ── Write outputs ─────────────────────────────────────────────────────────
    out_path = settings.output_dir / "patrol_routes.csv"
    df.to_csv(out_path, index=False)
    logger.info("Wrote patrol routes → %s", out_path)

    stats_path = settings.output_dir / "patrol_route_stats.csv"
    if ps_stats:
        pd.DataFrame(ps_stats).to_csv(stats_path, index=False)
        logger.info("Wrote patrol route stats → %s", stats_path)

    return df