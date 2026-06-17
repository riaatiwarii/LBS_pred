"""
LBS Predictor Optimization System — v4

Changes from v2:
  Fix 1: Phase 2 — max_frvs uses hard cap, not current+N (avoids false ceiling)
  Fix 2: Phase 1 — coverage_pct is incident-weighted, not medoid-count-weighted
  Fix 3: Phase 1/3 — "demand_score" renamed to "incident_volume" throughout
  Fix 4: Phase 2 — GEOGRAPHICALLY_CONSTRAINED flag when max_rt > 30 min
                   even if coverage escape hatch fires
  Fix 5: Phase 3 — statewide FRV gap numbers logged as primary output
  Fix 6: Phase 4 — RESOURCE_CONSTRAINED_OPTIMIZATION truly implemented:
                   incremental one-FRV-at-a-time allocation with priority
                   recalculation after each step

Pipeline:
    Incident Data
        ↓
    Phase 0: Data Validation
        ↓
    Phase 1: Baseline Analysis
        ↓
    Phase 2: Required FRV Estimation
        ↓
    Phase 3: Surplus/Deficit + Mode Detection + Statewide Gap Report
        ↓
    Phase 4: Redistribution (mode-aware — truly different behaviour)
        ↓
    Phase 5: Re-Simulation
        ↓
    Phase 6: District Summary
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from .clustering import (
    run_adaptive_hdbscan,
    allocate_frvs_to_clusters,
    sub_cluster_hotspot,
)
from .frv import load_frv_allocations
from .response_time import calculate_response_times
from .config import Settings

logger = logging.getLogger(__name__)

# Hard ceiling for FRV search in Phase 2.
# Prevents false "required = current + 20" conclusions for under-resourced PS.
# Tune this to the realistic maximum any single PS could ever receive.
_PHASE2_HARD_CAP = 60


# ============================================================================
# HELPERS
# ============================================================================


def _build_grid_from_ps(ps_incidents: pd.DataFrame) -> pd.DataFrame:
    """Aggregate incidents into a weighted spatial grid."""
    df = ps_incidents.copy()
    df["lat_grid"] = df["latitude"].round(3)
    df["lon_grid"] = df["longitude"].round(3)
    df["grid_key"] = df["lat_grid"].astype(str) + "_" + df["lon_grid"].astype(str)
    return df.groupby("grid_key").agg(
        latitude=("latitude", "mean"),
        longitude=("longitude", "mean"),
        weight=("latitude", "size"),
    ).reset_index()


def _compute_incident_volume(ps_incidents: pd.DataFrame) -> float:
    """
    Raw incident count.
    Intentionally named 'incident_volume' — not 'demand_score'.
    A real demand score would incorporate frequency, density, severity.
    This is purely volumetric and should be presented as such in reports.
    """
    return float(len(ps_incidents))


def build_demand_zones(
    grid: pd.DataFrame,
    settings: Settings,
) -> Tuple[Dict[int, Tuple[np.ndarray, np.ndarray]], Dict[int, int]]:
    """
    Discover demand zones from incident distribution alone.
    FRV count has zero influence here.

    HDBSCAN parameters (min_cluster_size, min_samples) come from settings.
    Cluster count emerges from incident density and spatial separation —
    urban PS naturally produces more zones than rural PS.

    Fallback 1: All-noise result → single zone covering all points.
    Fallback 2: Noise points → Euclidean nearest centroid (accepted tradeoff
                over road-based reassignment which is too expensive per-point).
    """
    labels, _, _ = run_adaptive_hdbscan(grid, n_frvs=None, settings=settings)
    grid = grid.copy()
    grid["local_label"] = labels
    cluster_ids = sorted(set(labels) - {-1})

    if len(cluster_ids) == 0:
        logger.warning(
            "  HDBSCAN returned zero clusters (all noise). "
            "Treating entire PS as single demand zone."
        )
        grid["local_label"] = 0
        cluster_ids = [0]
    elif (labels == -1).any():
        centroids = {
            cid: grid[grid["local_label"] == cid][["latitude", "longitude"]]
                     .mean().values
            for cid in cluster_ids
        }
        for idx in grid[grid["local_label"] == -1].index:
            point = grid.loc[idx, ["latitude", "longitude"]].values
            nearest = min(
                cluster_ids,
                key=lambda cid, p=point: np.linalg.norm(p - centroids[cid]),
            )
            grid.loc[idx, "local_label"] = nearest

    local_to_global = {label: i for i, label in enumerate(cluster_ids)}
    cluster_data: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
    cluster_sizes: Dict[int, int] = {}

    for local_label, global_label in local_to_global.items():
        cg = grid[grid["local_label"] == local_label]
        points  = cg[["latitude", "longitude"]].to_numpy()
        weights = cg["weight"].to_numpy()
        cluster_data[global_label]  = (points, weights)
        cluster_sizes[global_label] = int(weights.sum())

    return cluster_data, cluster_sizes


def _simulate(
    ps: str,
    district: str,
    cluster_data: Dict[int, Tuple[np.ndarray, np.ndarray]],
    cluster_sizes: Dict[int, int],
    n_frvs: int,
    settings: Settings,
) -> Tuple[float, float, float, np.ndarray]:
    """
    Allocate n_frvs across fixed demand zones, subdivide service areas,
    compute road-network response times.

    Returns (avg_rt, p90_rt, max_rt, incident_rt_array).

    P90 is incident-level — each zone expands its avg RT across all its
    incidents using the zone average because response_time.py does not yet
    generate per-incident response times.
    """
    allocation = allocate_frvs_to_clusters(cluster_sizes, n_frvs)
    medoids: Dict[int, dict] = {}
    medoid_zone_data: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
    gid = 0

    for cluster_id, k in allocation.items():
        if k <= 0:
            continue
        points, weights = cluster_data[cluster_id]
        zones = sub_cluster_hotspot(points, weights, k, settings)
        for zone in zones:
            medoids[gid] = {
                "district":       district,
                "police_station": ps,
                "latitude":       float(zone["medoid"][0]),
                "longitude":      float(zone["medoid"][1]),
                "size":           int(zone["size"]),
                "avg_radius_km":  float(zone["avg_radius_km"]),
                "max_radius_km":  float(zone["max_radius_km"]),
            }
            medoid_zone_data[gid] = (zone["points"], zone["weights"])
            gid += 1

    medoids = calculate_response_times(medoids, medoid_zone_data, settings)

    incident_rts: List[float] = []
    for m in medoids.values():
        rt = float(m.get("avg_response_time_min", 0.0))
        size = max(1, int(m.get("size", 1)))
        incident_rts.extend([rt] * size)

    if not incident_rts:
        inf = float("inf")
        return inf, inf, inf, np.array([])

    arr    = np.array(incident_rts)
    avg_rt = float(arr.mean())
    p90_rt = float(np.percentile(arr, 90))
    max_rt = float(arr.max())

    return avg_rt, p90_rt, max_rt, arr


# ============================================================================
# PHASE 0: DATA VALIDATION
# ============================================================================


def phase_0_data_validation(
    settings: Settings,
    incidents: pd.DataFrame,
    medoids: Dict[int, dict],
) -> Dict:
    """
    Validate inputs. Hard-stop on FRV mismatch.
    frv_df must have a 'ps' column — PS-level tracking is mandatory.
    Returns frv_by_ps carried forward to all downstream phases.
    """
    logger.info("")
    logger.info("=" * 73)
    logger.info("PHASE 0: DATA VALIDATION")
    logger.info("=" * 73)
    t0 = time.time()

    districts = [d for d in incidents["district"].dropna().unique() if d != "Outside MP"]
    ps_list   = [p for p in incidents["ps"].dropna().unique()       if p != "Outside PS"]

    frv_df, _, _ = load_frv_allocations(settings)

    if "ps" not in frv_df.columns:
        raise ValueError(
            "frv_df must have a 'ps' column. "
            "FRV allocations must be tracked at Police Station level. "
            "Fix load_frv_allocations() in frv.py."
        )

    frv_by_ps: Dict[str, List[str]] = (
        frv_df.groupby("ps")["frv_id"].apply(list).to_dict()
    )
    total_frvs = len(frv_df)
    sum_by_ps  = sum(len(v) for v in frv_by_ps.values())

    logger.info("Total Districts:          %d", len(districts))
    logger.info("Total Police Stations:    %d", len(ps_list))
    logger.info("Total FRVs (inventory):   %d", total_frvs)
    logger.info("Total Incidents:          %d", len(incidents))
    logger.info("Total Hotspots (medoids): %d", len(medoids))

    if sum_by_ps != total_frvs:
        msg = (
            f"FRV SUM MISMATCH: inventory={total_frvs}, sum_by_ps={sum_by_ps}. "
            "Fix FRV allocation data before proceeding."
        )
        logger.error("⚠️  %s", msg)
        raise RuntimeError(msg)

    logger.info("✓ FRV inventory validated (PS-level)")
    logger.info("Duration: %.2f seconds", time.time() - t0)
    logger.info("=" * 73 + "\n")

    return {
        "n_districts":  len(districts),
        "n_ps":         len(ps_list),
        "total_frvs":   total_frvs,
        "n_incidents":  len(incidents),
        "n_hotspots":   len(medoids),
        "frv_by_ps":    frv_by_ps,
    }


# ============================================================================
# PHASE 1: BASELINE ANALYSIS
# ============================================================================


def phase_1_baseline_analysis(
    settings: Settings,
    incidents: pd.DataFrame,
    medoids: Dict[int, dict],          # kept in signature for API compatibility
    frv_by_ps: Dict[str, List[str]],
) -> pd.DataFrame:
    """
    Baseline metrics per PS — uses the same road-network simulation engine
    as Phase 2 and Phase 5.

    ROOT-CAUSE FIX (v4):
        The previous implementation looked up medoids by
        ``m.get("police_station") == ps``.  Every medoid produced by
        run_district_level_clustering() stores ``"police_station":
        "District-level"``, so the lookup always returned an empty list and
        avg_rt / max_rt / coverage_pct were always 0.  This caused:
            • Failure 1 — baseline all zeros
            • Failure 2 — improvement_pct always 0  (0 - after ≈ 0)
            • Failure 3 — Phase 2 stop condition fired at n=1 FRV
                          because avg_rt=0 already satisfies <=10 min

        Fix: build demand zones from raw incidents (same as Phase 2) and
        call _simulate() with the PS's *current* FRV count.  The ``medoids``
        parameter is no longer used and is kept only so callers need not
        change their call site.

    Other design choices retained from v3:
        • coverage_pct is incident-weighted (not medoid-count-weighted)
        • column name is 'incident_volume' (not 'demand_score')
    """
    logger.info("")
    logger.info("=" * 73)
    logger.info("PHASE 1: BASELINE ANALYSIS")
    logger.info("=" * 73)
    t0 = time.time()

    ps_list = sorted([p for p in incidents["ps"].dropna().unique() if p != "Outside PS"])
    rows = []

    for idx, ps in enumerate(ps_list, 1):
        ps_inc   = incidents[incidents["ps"] == ps]
        district = ps_inc["district"].iloc[0] if len(ps_inc) > 0 else "Unknown"

        current_frvs    = len(frv_by_ps.get(ps, []))
        incident_volume = _compute_incident_volume(ps_inc)

        if len(ps_inc) >= settings.min_cluster_size and current_frvs > 0:
            grid = _build_grid_from_ps(ps_inc)
            cluster_data, cluster_sizes = build_demand_zones(grid, settings)
            avg_rt, _, max_rt, rt_arr = _simulate(
                ps, district, cluster_data, cluster_sizes, current_frvs, settings
            )
            total    = len(rt_arr)
            covered  = int((rt_arr <= 10.0).sum()) if total > 0 else 0
            cov_pct  = covered / total * 100 if total > 0 else 0.0
        elif len(ps_inc) >= settings.min_cluster_size and current_frvs == 0:
            # Has incidents but no FRVs — worst-case baseline
            avg_rt = max_rt = float("inf")
            cov_pct = 0.0
            # Store as a large finite value so downstream maths doesn't break
            avg_rt = max_rt = 999.0
        else:
            # Insufficient incidents for simulation
            avg_rt = max_rt = cov_pct = 0.0

        rows.append({
            "district":              district,
            "ps":                    ps,
            "current_frvs":          current_frvs,
            "incident_volume":       incident_volume,
            "avg_response_time_min": round(avg_rt, 2),
            "max_response_time_min": round(max_rt, 2),
            "coverage_pct":          round(cov_pct, 2),
        })

        if idx % 10 == 0 or idx == len(ps_list):
            logger.info("[%d/%d PS processed]", idx, len(ps_list))

    df = pd.DataFrame(rows)
    out = settings.output_dir / "01_baseline_analysis.csv"
    df.to_csv(out, index=False)
    logger.info("Wrote baseline → %s", out)

    logger.info("\n[BASELINE SAMPLE]")
    for r in df.head(5).itertuples(index=False):
        logger.info(
            "  %-25s | FRVs=%2d | Vol=%4.0f | "
            "AvgRT=%5.1f | MaxRT=%5.1f | Cov=%4.1f%%",
            r.ps, r.current_frvs, r.incident_volume,
            r.avg_response_time_min, r.max_response_time_min, r.coverage_pct,
        )

    logger.info("Duration: %.2f seconds", time.time() - t0)
    logger.info("=" * 73 + "\n")
    return df


# ============================================================================
# PHASE 2: REQUIRED FRV ESTIMATION
# ============================================================================


def phase_2_required_frv_estimation(
    settings: Settings,
    incidents: pd.DataFrame,
    baseline_df: pd.DataFrame,
    target_avg_rt_primary:   float = 10.0,
    target_avg_rt_secondary: float = 5.0,
) -> pd.DataFrame:
    """
    For every PS, iterate FRVs from 1 → _PHASE2_HARD_CAP.
    Demand zones built ONCE from incident distribution (no FRV influence).
    Only FRV allocation and service-zone subdivision vary.

    FIX: max search range is now _PHASE2_HARD_CAP (module constant = 60),
    not current_frvs + 20. Prevents false ceilings for under-resourced PS.

    Stop conditions:
        Primary   (10 min): avg_rt <= 10 AND p90_rt <= 15
                            OR avg_rt <= 10 AND incident_coverage_10min >= 90%
        Secondary  (5 min): avg_rt <=  5 AND p90_rt <=  8
                            OR avg_rt <=  5 AND incident_coverage_5min  >= 90%

    FIX: if max_rt > 30 when escape hatch fires, PS is flagged
    GEOGRAPHICALLY_CONSTRAINED instead of silently accepted. This surfaces
    outlier villages that are still waiting 30+ min despite passing coverage %.

    Logs full simulation table per PS for traceability.
    """
    logger.info("")
    logger.info("=" * 73)
    logger.info(
        "PHASE 2: REQUIRED FRV ESTIMATION  "
        "(Primary: %.0f min | Secondary: %.0f min | Hard cap: %d FRVs)",
        target_avg_rt_primary, target_avg_rt_secondary, _PHASE2_HARD_CAP,
    )
    logger.info("=" * 73)
    t0 = time.time()

    ps_list = sorted([p for p in incidents["ps"].dropna().unique() if p != "Outside PS"])
    rows = []

    for idx, ps in enumerate(ps_list, 1):
        ps_inc   = incidents[incidents["ps"] == ps]
        district = ps_inc["district"].iloc[0] if len(ps_inc) > 0 else "Unknown"

        bl_row       = baseline_df[baseline_df["ps"] == ps]
        current_frvs = int(bl_row["current_frvs"].iloc[0]) if len(bl_row) > 0 else 0

        # ── Insufficient data ────────────────────────────────────────────────
        if len(ps_inc) < settings.min_cluster_size:
            rows.append({
                "district":            district,
                "ps":                  ps,
                "current_frvs":        current_frvs,
                "required_frvs_10min": current_frvs,
                "required_frvs_5min":  current_frvs,
                "n_demand_zones":      0,
                "geo_constrained":     False,
                "reason":              "Insufficient incidents — kept current allocation",
            })
            continue

        # ── Build demand zones ONCE (no FRV influence) ──────────────────────
        grid = _build_grid_from_ps(ps_inc)
        cluster_data, cluster_sizes = build_demand_zones(grid, settings)
        n_zones = len(cluster_data)

        logger.info("")
        logger.info("  " + "=" * 60)
        logger.info("  PS: %-30s  District: %s", ps, district)
        logger.info("  Demand Zones: %d   Current FRVs: %d", n_zones, current_frvs)
        logger.info("  %-6s  %-8s  %-8s  %-8s", "FRVs", "Avg RT", "P90 RT", "Max RT")
        logger.info("  " + "-" * 40)

        required_10min:  int | None = None
        required_5min:   int | None = None
        geo_constrained: bool       = False

        for n in range(1, _PHASE2_HARD_CAP + 1):
            avg_rt, p90_rt, max_rt, rt_arr = _simulate(
                ps, district, cluster_data, cluster_sizes, n, settings
            )

            logger.info(
                "  %-6d  %-8.1f  %-8.1f  %-8.1f", n, avg_rt, p90_rt, max_rt
            )

            cov_10 = float((rt_arr <= 10.0).mean() * 100) if len(rt_arr) else 0.0
            cov_5  = float((rt_arr <=  5.0).mean() * 100) if len(rt_arr) else 0.0

            # Primary stop
            if required_10min is None:
                strict_pass  = avg_rt <= target_avg_rt_primary and p90_rt <= 15.0
                # FIX: coverage escape hatch fires but we check max_rt too
                coverage_pass = avg_rt <= target_avg_rt_primary and cov_10 >= 90.0
                if strict_pass or coverage_pass:
                    required_10min = n
                    if coverage_pass and not strict_pass and max_rt > 30.0:
                        geo_constrained = True
                        logger.warning(
                            "  ⚠  GEOGRAPHICALLY_CONSTRAINED: "
                            "coverage escape hatch fired but max_rt=%.1f min (>30). "
                            "Remote area still severely underserved.", max_rt
                        )

            # Secondary stop
            if required_5min is None:
                if (avg_rt <= target_avg_rt_secondary and p90_rt <= 8.0
                        or avg_rt <= target_avg_rt_secondary and cov_5 >= 90.0):
                    required_5min = n

            if required_10min is not None and required_5min is not None:
                break

        if required_10min is None:
            required_10min = _PHASE2_HARD_CAP
            logger.warning(
                "  10-min target NOT reached within hard cap (%d FRVs)", _PHASE2_HARD_CAP
            )
        if required_5min is None:
            required_5min = _PHASE2_HARD_CAP
            logger.warning(
                "   5-min target NOT reached within hard cap (%d FRVs)", _PHASE2_HARD_CAP
            )

        logger.info(
            "  Required(10min)=%d   Required(5min)=%d   GeoConstrained=%s",
            required_10min, required_5min, geo_constrained,
        )
        logger.info("  " + "=" * 60)

        rows.append({
            "district":            district,
            "ps":                  ps,
            "current_frvs":        current_frvs,
            "required_frvs_10min": required_10min,
            "required_frvs_5min":  required_5min,
            "n_demand_zones":      n_zones,
            "geo_constrained":     geo_constrained,
            "reason":              "Iterative road-network simulation",
        })

        logger.info("[%d/%d] PS=%s | Req(10min)=%d | Req(5min)=%d",
                    idx, len(ps_list), ps, required_10min, required_5min)

    df = pd.DataFrame(rows)
    out = settings.output_dir / "02_required_frv_estimation.csv"
    df.to_csv(out, index=False)
    logger.info("Wrote requirements → %s", out)
    logger.info("Duration: %.2f seconds", time.time() - t0)
    logger.info("=" * 73 + "\n")
    return df


# ============================================================================
# PHASE 3: SURPLUS / DEFICIT  +  MODE DETECTION  +  STATEWIDE GAP REPORT
# ============================================================================


def phase_3_surplus_deficit_analysis(
    requirement_df: pd.DataFrame,
    baseline_df: pd.DataFrame,
    settings: Settings,
) -> Tuple[pd.DataFrame, str]:
    """
    Compute surplus/deficit, detect optimization mode, report statewide gap.

    FIX: statewide gap numbers are now the primary logged output.
    total_required_10min, total_required_5min, additional_needed_10min are
    the most actionable numbers in the entire pipeline — log them prominently.

    FIX: 'demand_score' → 'incident_volume' throughout to match Phase 1.

    MODE DETECTION:
        total_required_10min <= total_available → TARGET_SATISFACTION
        total_required_10min >  total_available → RESOURCE_CONSTRAINED_OPTIMIZATION

    These modes produce genuinely different Phase 4 behaviour.
    """
    logger.info("")
    logger.info("=" * 73)
    logger.info("PHASE 3: SURPLUS/DEFICIT ANALYSIS")
    logger.info("=" * 73)
    t0 = time.time()

    df = requirement_df.merge(
        baseline_df[["ps", "incident_volume", "avg_response_time_min"]],
        on="ps", how="left",
    )

    df["surplus_deficit"] = df["current_frvs"] - df["required_frvs_10min"]
    df["type"] = df["surplus_deficit"].apply(
        lambda x: "SURPLUS" if x > 0 else ("DEFICIT" if x < 0 else "BALANCED")
    )

    surplus  = df[df["type"] == "SURPLUS"]
    deficit  = df[df["type"] == "DEFICIT"]
    balanced = df[df["type"] == "BALANCED"]

    total_available        = int(df["current_frvs"].sum())
    total_required_10min   = int(df["required_frvs_10min"].sum())
    total_required_5min    = int(df["required_frvs_5min"].sum())
    additional_needed_10min = max(0, total_required_10min - total_available)
    additional_needed_5min  = max(0, total_required_5min  - total_available)
    total_transferable     = int(surplus["surplus_deficit"].sum())
    total_needed           = int(abs(deficit["surplus_deficit"].sum()))

    # ── Mode detection ───────────────────────────────────────────────────────
    mode = (
        "TARGET_SATISFACTION"
        if total_required_10min <= total_available
        else "RESOURCE_CONSTRAINED_OPTIMIZATION"
    )

    # FIX: statewide gap is the headline number — log it first and prominently
    logger.info("")
    logger.info("  ┌─────────────────────────────────────────────┐")
    logger.info("  │           STATEWIDE FRV GAP REPORT          │")
    logger.info("  ├─────────────────────────────────────────────┤")
    logger.info("  │  FRVs currently available:  %4d             │", total_available)
    logger.info("  │  FRVs required (10-min):    %4d             │", total_required_10min)
    logger.info("  │  FRVs required  (5-min):    %4d             │", total_required_5min)
    logger.info("  │  Additional needed (10min): %4d             │", additional_needed_10min)
    logger.info("  │  Additional needed  (5min): %4d             │", additional_needed_5min)
    logger.info("  │  Optimization mode: %-24s │", mode)
    logger.info("  └─────────────────────────────────────────────┘")
    logger.info("")
    logger.info("Surplus PS:  %d  (%d FRVs transferable)", len(surplus), total_transferable)
    logger.info("Deficit PS:  %d  (%d FRVs needed)",       len(deficit), total_needed)
    logger.info("Balanced PS: %d",                          len(balanced))

    logger.info("\n[SURPLUS — top 5]")
    for r in surplus.nlargest(5, "surplus_deficit").itertuples(index=False):
        logger.info(
            "  [SURPLUS] %-25s | Cur=%2d | Req=%2d | Transfer=%2d",
            r.ps, r.current_frvs, r.required_frvs_10min, r.surplus_deficit,
        )

    logger.info("\n[DEFICIT — top 5]")
    for r in deficit.nsmallest(5, "surplus_deficit").itertuples(index=False):
        logger.info(
            "  [DEFICIT] %-25s | Cur=%2d | Req=%2d | Need=%2d",
            r.ps, r.current_frvs, r.required_frvs_10min, abs(r.surplus_deficit),
        )

    out = settings.output_dir / "03_surplus_deficit_analysis.csv"
    df.to_csv(out, index=False)
    logger.info("\nWrote surplus/deficit → %s", out)
    logger.info("Duration: %.2f seconds", time.time() - t0)
    logger.info("=" * 73 + "\n")

    return df, mode


# ============================================================================
# PHASE 4: REDISTRIBUTION  (mode-aware — genuinely different behaviour)
# ============================================================================


def _priority_score(avg_rt: float, incident_volume: float, deficit: float,
                    max_rt_norm: float, max_deficit: float, max_volume: float) -> float:
    """
    Composite priority score for RESOURCE_CONSTRAINED mode.
    Higher score = gets the next available FRV.

    Weights:
        50% — avg response time (worst-served first)
        30% — incident volume   (busiest first)
        20% — deficit size      (most under-resourced first)

    All inputs normalised to [0, 1] before weighting.
    """
    norm_rt  = avg_rt          / max(max_rt_norm,   0.01)
    norm_vol = incident_volume / max(max_volume,     0.01)
    norm_def = abs(deficit)    / max(max_deficit,    0.01)
    return 0.5 * norm_rt + 0.3 * norm_vol + 0.2 * norm_def


def phase_4_redistribution(
    surplus_deficit_df: pd.DataFrame,
    mode: str,
    settings: Settings,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Redistribute FRVs from surplus PS to deficit PS.

    TARGET_SATISFACTION mode:
        Standard fill: move FRVs until each deficit PS reaches its required_10min.
        Donor never drops below required_frvs_10min.

    RESOURCE_CONSTRAINED_OPTIMIZATION mode:
        FIX: genuinely different behaviour from previous version.
        Allocates one FRV at a time to the highest-priority deficit PS.
        Priority recalculated after each allocation using composite score:
            50% avg_rt  +  30% incident_volume  +  20% deficit_size
        This maximises statewide RT improvement under the fixed FRV budget
        rather than just filling deficits in list order.
        Donor floor: required_frvs_10min (best-effort — donor may already
        be below target if total budget is insufficient).
    """
    logger.info("")
    logger.info("=" * 73)
    logger.info("PHASE 4: REDISTRIBUTION  [mode: %s]", mode)
    logger.info("=" * 73)
    t0 = time.time()

    df = surplus_deficit_df.copy()
    df["current_frvs_after"] = df["current_frvs"].copy()

    donors = (
        df[df["type"] == "SURPLUS"]
        .sort_values("surplus_deficit", ascending=False)
        .to_dict("records")
    )
    deficit_records = df[df["type"] == "DEFICIT"].to_dict("records")

    donor_available: Dict[str, int] = {d["ps"]: d["surplus_deficit"] for d in donors}
    transfers: List[dict] = []

    total_pool = sum(donor_available.values())
    logger.info("Total FRVs available for transfer: %d", total_pool)

    # ── TARGET_SATISFACTION ──────────────────────────────────────────────────
    if mode == "TARGET_SATISFACTION":
        receivers = sorted(
            deficit_records,
            key=lambda r: (
                -r["avg_response_time_min"],
                -r["incident_volume"],
                r["surplus_deficit"],
            ),
        )
        for recv in receivers:
            need    = abs(recv["surplus_deficit"])
            recv_ps = recv["ps"]
            for donor in donors:
                if need <= 0:
                    break
                donor_ps  = donor["ps"]
                available = donor_available.get(donor_ps, 0)
                if available <= 0:
                    continue
                move = min(available, int(need))
                donor_available[donor_ps] -= move
                need -= move
                df.loc[df["ps"] == donor_ps, "current_frvs_after"] -= move
                df.loc[df["ps"] == recv_ps,  "current_frvs_after"] += move
                transfers.append({
                    "donor_district":             donor["district"],
                    "donor_ps":                   donor_ps,
                    "receiver_district":          recv["district"],
                    "receiver_ps":                recv_ps,
                    "frvs_moved":                 move,
                    "donor_surplus_remaining":    donor_available[donor_ps],
                    "receiver_deficit_remaining": need,
                    "mode":                       mode,
                })
                logger.info(
                    "[TRANSFER] %-22s → %-22s | %2d FRVs | "
                    "Donor left=%2d | Recv deficit left=%2d",
                    donor_ps, recv_ps, move,
                    donor_available[donor_ps], need,
                )

    # ── RESOURCE_CONSTRAINED_OPTIMIZATION ───────────────────────────────────
    else:
        # Build a live state dict for every deficit PS
        # Tracks current FRV count after each incremental allocation
        recv_state: Dict[str, dict] = {
            r["ps"]: {
                "ps":               r["ps"],
                "district":         r["district"],
                "deficit":          r["surplus_deficit"],        # negative number
                "frvs_current":     r["current_frvs"],
                "avg_rt":           r["avg_response_time_min"],
                "incident_volume":  r["incident_volume"],
                "required":         r["required_frvs_10min"],
            }
            for r in deficit_records
        }

        # Normalisation denominators (fixed — computed once)
        max_rt     = max(s["avg_rt"]          for s in recv_state.values()) or 1.0
        max_vol    = max(s["incident_volume"]  for s in recv_state.values()) or 1.0
        max_def    = max(abs(s["deficit"])     for s in recv_state.values()) or 1.0

        frvs_remaining = sum(donor_available.values())

        while frvs_remaining > 0:
            # Pick the highest-priority deficit PS that still needs FRVs
            eligible = [
                s for s in recv_state.values()
                if s["frvs_current"] < s["required"]
            ]
            if not eligible:
                break  # All deficits satisfied

            eligible.sort(
                key=lambda s: _priority_score(
                    s["avg_rt"], s["incident_volume"], s["deficit"],
                    max_rt, max_def, max_vol,
                ),
                reverse=True,
            )
            recv = eligible[0]
            recv_ps = recv["ps"]

            # Find a donor with available surplus
            donor_chosen = None
            for donor in donors:
                if donor_available.get(donor["ps"], 0) > 0:
                    donor_chosen = donor
                    break

            if donor_chosen is None:
                break  # Pool exhausted

            donor_ps = donor_chosen["ps"]
            donor_available[donor_ps] -= 1
            frvs_remaining -= 1

            recv_state[recv_ps]["frvs_current"] += 1
            recv_state[recv_ps]["deficit"]      += 1   # deficit shrinks by 1

            df.loc[df["ps"] == donor_ps, "current_frvs_after"] -= 1
            df.loc[df["ps"] == recv_ps,  "current_frvs_after"] += 1

            deficit_left = abs(recv_state[recv_ps]["deficit"])
            transfers.append({
                "donor_district":             donor_chosen["district"],
                "donor_ps":                   donor_ps,
                "receiver_district":          recv["district"],
                "receiver_ps":                recv_ps,
                "frvs_moved":                 1,
                "donor_surplus_remaining":    donor_available[donor_ps],
                "receiver_deficit_remaining": deficit_left,
                "mode":                       mode,
            })

            logger.info(
                "[TRANSFER] %-22s → %-22s | 1 FRV | "
                "Priority=%.3f | Donor left=%2d | Recv deficit left=%2d",
                donor_ps, recv_ps,
                _priority_score(
                    recv["avg_rt"], recv["incident_volume"], recv["deficit"],
                    max_rt, max_def, max_vol,
                ),
                donor_available[donor_ps], deficit_left,
            )

    transfer_df = pd.DataFrame(transfers)
    out = settings.output_dir / "04_redistribution_transfers.csv"
    transfer_df.to_csv(out, index=False)
    logger.info("\nWrote %d transfers → %s", len(transfer_df), out)
    logger.info("Duration: %.2f seconds", time.time() - t0)
    logger.info("=" * 73 + "\n")

    return df, transfer_df


# ============================================================================
# PHASE 5: RE-SIMULATION
# ============================================================================


def phase_5_resimulation(
    settings: Settings,
    incidents: pd.DataFrame,
    redistribution_df: pd.DataFrame,
    baseline_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Re-simulate with redistributed FRV counts.
    before_avg_rt pulled from baseline_df — never from redistribution_df.
    Reports avg, p90, max RT before and after for every PS.
    """
    logger.info("")
    logger.info("=" * 73)
    logger.info("PHASE 5: RE-SIMULATION")
    logger.info("=" * 73)
    t0 = time.time()

    baseline_rt: Dict[str, float] = (
        baseline_df.set_index("ps")["avg_response_time_min"].to_dict()
    )

    rows = []
    ps_list = sorted(redistribution_df["ps"].unique())

    for idx, ps in enumerate(ps_list, 1):
        ps_inc   = incidents[incidents["ps"] == ps]
        district = ps_inc["district"].iloc[0] if len(ps_inc) > 0 else "Unknown"

        row_data      = redistribution_df[redistribution_df["ps"] == ps].iloc[0]
        new_frvs      = int(row_data["current_frvs_after"])
        old_frvs      = int(row_data["current_frvs"])
        before_avg_rt = baseline_rt.get(ps, 0.0)

        if len(ps_inc) < settings.min_cluster_size:
            rows.append({
                "district": district, "ps": ps,
                "before_frvs": old_frvs, "after_frvs": new_frvs,
                "before_avg_response_min": before_avg_rt,
                "after_avg_response_min":  before_avg_rt,
                "after_p90_response_min":  before_avg_rt,
                "after_max_response_min":  before_avg_rt,
                "improvement_pct":         0.0,
                "reason":                  "Insufficient incidents",
            })
            continue

        grid = _build_grid_from_ps(ps_inc)
        cluster_data, cluster_sizes = build_demand_zones(grid, settings)

        after_avg_rt, after_p90_rt, after_max_rt, _ = _simulate(
            ps, district, cluster_data, cluster_sizes, new_frvs, settings
        )

        # 999.0 sentinel means "had incidents but zero FRVs in baseline".
        # Use a finite reference so improvement_pct is meaningful.
        _before_ref = before_avg_rt if before_avg_rt < 900 else after_avg_rt * 2
        if _before_ref > 0 and after_avg_rt < float("inf"):
            improvement_pct = round(
                (_before_ref - after_avg_rt) / max(_before_ref, 0.01) * 100, 2
            )
        else:
            improvement_pct = 0.0

        rows.append({
            "district":                district,
            "ps":                      ps,
            "before_frvs":             old_frvs,
            "after_frvs":              new_frvs,
            "before_avg_response_min": before_avg_rt if before_avg_rt < 900 else None,
            "after_avg_response_min":  after_avg_rt,
            "after_p90_response_min":  after_p90_rt,
            "after_max_response_min":  after_max_rt,
            "improvement_pct":         improvement_pct,
        })

        if idx % 10 == 0 or idx == len(ps_list):
            logger.info("[%d/%d PS processed]", idx, len(ps_list))

    df = pd.DataFrame(rows)
    out = settings.output_dir / "05_resimulation_results.csv"
    df.to_csv(out, index=False)
    logger.info("Wrote re-simulation results → %s", out)

    logger.info("\n[TOP IMPROVEMENTS]")
    for r in df.nlargest(5, "improvement_pct").itertuples(index=False):
        logger.info(
            "  %-25s | Before=%5.1f → After=%5.1f | P90=%5.1f | Max=%5.1f | Δ=%+.1f%%",
            r.ps, r.before_avg_response_min, r.after_avg_response_min,
            r.after_p90_response_min, r.after_max_response_min, r.improvement_pct,
        )

    logger.info("Duration: %.2f seconds", time.time() - t0)
    logger.info("=" * 73 + "\n")
    return df


# ============================================================================
# PHASE 6: DISTRICT OPTIMIZATION SUMMARY
# ============================================================================


def phase_6_district_optimization_summary(
    resim_df: pd.DataFrame,
    settings: Settings,
) -> pd.DataFrame:
    """District-level rollup of before/after metrics including P90 and max."""
    logger.info("")
    logger.info("=" * 73)
    logger.info("PHASE 6: DISTRICT OPTIMIZATION SUMMARY")
    logger.info("=" * 73)
    t0 = time.time()

    rows = []
    for district in resim_df["district"].unique():
        d          = resim_df[resim_df["district"] == district]
        before_avg = d["before_avg_response_min"].mean()
        after_avg  = d["after_avg_response_min"].mean()
        improvement_pct = (
            (before_avg - after_avg) / max(before_avg, 0.01) * 100
            if before_avg > 0 else 0.0
        )
        rows.append({
            "district":                district,
            "ps_count":                len(d),
            "before_total_frvs":       d["before_frvs"].sum(),
            "after_total_frvs":        d["after_frvs"].sum(),
            "before_avg_response_min": before_avg,
            "after_avg_response_min":  after_avg,
            "after_avg_p90_min":       d["after_p90_response_min"].mean(),
            "after_avg_max_min":       d["after_max_response_min"].mean(),
            "improvement_pct":         improvement_pct,
        })

    df = pd.DataFrame(rows).sort_values("improvement_pct", ascending=False)
    out = settings.output_dir / "06_district_optimization_summary.csv"
    df.to_csv(out, index=False)
    logger.info("Wrote district summary → %s", out)

    logger.info("\n[DISTRICT IMPROVEMENTS]")
    for r in df.itertuples(index=False):
        logger.info(
            "  %-22s | Before=%5.1f → After=%5.1f | P90=%5.1f | Δ=%+.1f%% | PS=%d",
            r.district, r.before_avg_response_min, r.after_avg_response_min,
            r.after_avg_p90_min, r.improvement_pct, r.ps_count,
        )

    logger.info("Duration: %.2f seconds", time.time() - t0)
    logger.info("=" * 73 + "\n")
    return df


# ============================================================================
# MAIN ORCHESTRATION
# ============================================================================


def run_full_optimization(
    settings: Settings,
    incidents: pd.DataFrame,
    medoids: Dict[int, dict],
) -> Dict[str, object]:
    """Run complete optimization pipeline."""
    logger.info("\n")
    logger.info("#" * 73)
    logger.info("# LBS PREDICTOR OPTIMIZATION SYSTEM  v4")
    logger.info("# Start: %s", datetime.now().isoformat())
    logger.info("#" * 73)

    validation  = phase_0_data_validation(settings, incidents, medoids)
    frv_by_ps   = validation["frv_by_ps"]

    baseline_df = phase_1_baseline_analysis(settings, incidents, medoids, frv_by_ps)

    requirement_df = phase_2_required_frv_estimation(
        settings, incidents, baseline_df,
        target_avg_rt_primary=10.0,
        target_avg_rt_secondary=5.0,
    )

    surplus_deficit_df, mode = phase_3_surplus_deficit_analysis(
        requirement_df, baseline_df, settings
    )

    redistribution_df, transfer_df = phase_4_redistribution(
        surplus_deficit_df, mode, settings
    )

    resim_df = phase_5_resimulation(
        settings, incidents, redistribution_df, baseline_df
    )

    district_summary_df = phase_6_district_optimization_summary(resim_df, settings)

    # ── Post-optimization validation report ─────────────────────────────────
    _baseline_valid = baseline_df[baseline_df["avg_response_time_min"] < 900]
    _resim_valid    = resim_df[resim_df["before_avg_response_min"].notna()]

    _before_mean = _resim_valid["before_avg_response_min"].mean() if len(_resim_valid) > 0 else float("nan")
    _after_mean  = _resim_valid["after_avg_response_min"].mean()  if len(_resim_valid) > 0 else float("nan")
    _ps_meeting_before = int((_baseline_valid["avg_response_time_min"] <= 10).sum())
    _ps_meeting_after  = int((resim_df["after_avg_response_min"] <= 10).sum())

    logger.info("")
    logger.info("  ┌─────────────────────────────────────────────────────┐")
    logger.info("  │            POST-OPTIMIZATION VALIDATION             │")
    logger.info("  ├─────────────────────────────────────────────────────┤")
    logger.info("  │  Total PS evaluated:          %4d                  │", len(resim_df))
    logger.info("  │  Total FRVs available:        %4d                  │", int(baseline_df["current_frvs"].sum()))
    logger.info("  │  State avg RT  — before:   %6.1f min              │", _before_mean)
    logger.info("  │  State avg RT  — after:    %6.1f min              │", _after_mean)
    logger.info("  │  PS meeting ≤10 min before:   %4d                  │", _ps_meeting_before)
    logger.info("  │  PS meeting ≤10 min after:    %4d                  │", _ps_meeting_after)
    logger.info("  │  Transfers executed:          %4d                  │", len(transfer_df))
    logger.info("  │  Optimization mode: %-31s │", mode)
    logger.info("  └─────────────────────────────────────────────────────┘")

    if abs(_before_mean - _after_mean) < 0.1 and len(transfer_df) > 10:
        logger.warning(
            "  ⚠ WARNING: %d transfers executed but state avg RT unchanged "
            "(before=%.1f, after=%.1f). "
            "Check whether redistribution_df 'current_frvs_after' differs "
            "from 'current_frvs' for affected PS.",
            len(transfer_df), _before_mean, _after_mean,
        )

    logger.info("#" * 73)
    logger.info("# OPTIMIZATION COMPLETE — Mode: %s", mode)
    logger.info("# End: %s", datetime.now().isoformat())
    logger.info("#" * 73 + "\n")

    return {
        "validation":           validation,
        "baseline_df":          baseline_df,
        "requirement_df":       requirement_df,
        "surplus_deficit_df":   surplus_deficit_df,
        "mode":                 mode,
        "redistribution_df":    redistribution_df,
        "transfer_df":          transfer_df,
        "resim_df":             resim_df,
        "district_summary_df":  district_summary_df,
    }


# Backward-compatible alias for pipeline import naming
run_full_optimization_refactored = run_full_optimization