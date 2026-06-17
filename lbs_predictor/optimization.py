from __future__ import annotations

import logging
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from .clustering import (
    run_adaptive_hdbscan,
    allocate_frvs_to_clusters,
    sub_cluster_hotspot,
)
from .clustering import compute_medoid
from .frv import load_frv_allocations
from .response_time import calculate_response_times
from .config import Settings
from .response_time import aerial_distance_km

logger = logging.getLogger(__name__)


def _build_grid_from_district(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["lat_grid"] = df["latitude"].round(3)
    df["lon_grid"] = df["longitude"].round(3)
    df["grid_key"] = df["lat_grid"].astype(str) + "_" + df["lon_grid"].astype(str)
    grid = df.groupby("grid_key").agg(
        latitude=("latitude", "mean"), longitude=("longitude", "mean"), weight=("latitude", "size")
    ).reset_index()
    return grid


def estimate_frv_requirements(
    settings: Settings,
    incidents: pd.DataFrame,
    target_avg_rt: float = 8.0,
    max_extra_frvs: int = 20,
) -> pd.DataFrame:
    """Estimate required FRVs per district using simulation.

    Returns a DataFrame with columns: district, current_frvs, required_frvs
    """
    frv_df, district_counts, frv_by_district = load_frv_allocations(settings)

    results = []
    for district in sorted(d for d in incidents["district"].dropna().unique() if d != "Outside MP"):
        dist_mask = incidents[incidents["district"] == district]
        current = district_counts.get(district, 0)
        if len(dist_mask) < settings.min_cluster_size:
            results.append({"district": district, "current_frvs": current, "required_frvs": current})
            continue

        grid = _build_grid_from_district(dist_mask)

        required = current
        # Try incremental increases
        for total in range(max(1, current), current + max_extra_frvs + 1):
            # Run adaptive HDBSCAN to get local clusters (labels)
            labels, adaptive_min_size, n_clusters = run_adaptive_hdbscan(grid, total, settings)
            grid["local_label"] = labels
            local_clusters = sorted(set(labels) - {-1})
            local_to_global = {label: i for i, label in enumerate(local_clusters)}

            cluster_sizes = {}
            cluster_data = {}
            for local_label, global_label in local_to_global.items():
                cluster_grid = grid[grid["local_label"] == local_label]
                points = cluster_grid[["latitude", "longitude"]].to_numpy()
                weights = cluster_grid["weight"].to_numpy()
                cluster_sizes[global_label] = int(weights.sum())
                cluster_data[global_label] = (points, weights)

            allocation = allocate_frvs_to_clusters(cluster_sizes, total)
            medoids: Dict[int, dict] = {}
            medoid_zone_data: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
            global_medoid_id = 0
            frv_idx = 0
            for cluster_id, k in allocation.items():
                if k <= 0:
                    continue
                points, weights = cluster_data[cluster_id]
                zones = sub_cluster_hotspot(points, weights, k, settings)
                for zone_idx, zone in enumerate(zones):
                    medoids[global_medoid_id] = {
                        "district": district,
                        "police_station": "District-level",
                        "hotspot_id": cluster_id,
                        "sub_zone": zone_idx + 1,
                        "sub_zones_total": len(zones),
                        "frv_id": f"SIM-{total}-{frv_idx}",
                        "latitude": float(zone["medoid"][0]),
                        "longitude": float(zone["medoid"][1]),
                        "size": int(zone["size"]),
                        "grid_cells": int(zone["grid_cells"]),
                        "avg_radius_km": float(zone["avg_radius_km"]),
                        "max_radius_km": float(zone["max_radius_km"]),
                    }
                    medoid_zone_data[global_medoid_id] = (zone["points"], zone["weights"])
                    global_medoid_id += 1
                    frv_idx += 1

            # Compute response times for these medoids
            medoids = calculate_response_times(medoids, medoid_zone_data, settings)

            # Compute weighted average response time across medoids
            times = []
            weights = []
            for m in medoids.values():
                size = float(m.get("size", 0))
                rt = float(m.get("avg_response_time_min", 0.0))
                times.append(rt)
                weights.append(max(1.0, size))
            if not times:
                avg_rt = float("inf")
            else:
                avg_rt = float(np.average(times, weights=weights))

            if avg_rt <= target_avg_rt:
                required = total
                break

        results.append({"district": district, "current_frvs": current, "required_frvs": required})

    res_df = pd.DataFrame(results)
    out_path = settings.output_dir / "frv_requirements.csv"
    res_df.to_csv(out_path, index=False)
    logger.info("Wrote FRV requirements to %s", out_path)
    return res_df


def propose_district_transfers(req_df: pd.DataFrame) -> pd.DataFrame:
    """Greedy proposer: match surplus districts to deficit districts.

    Returns DataFrame with columns: from_district, to_district, n_frvs
    """
    df = req_df.copy()
    df["surplus"] = df["current_frvs"] - df["required_frvs"]
    donors = df[df["surplus"] > 0].copy().sort_values("surplus", ascending=False)
    receivers = df[df["surplus"] < 0].copy()

    transfers: List[Tuple[str, str, int]] = []
    donor_idx = 0
    donor_list = donors.to_dict("records")
    for recv in receivers.to_dict("records"):
        need = -int(recv["surplus"])
        for d in donor_list:
            if need <= 0:
                break
            avail = int(d["surplus"])
            if avail <= 0:
                continue
            take = min(avail, need)
            transfers.append((d["district"], recv["district"], take))
            d["surplus"] -= take
            need -= take

    transfer_df = pd.DataFrame(transfers, columns=["from_district", "to_district", "n_frvs"]).groupby(["from_district", "to_district"]).sum().reset_index()
    return transfer_df


def estimate_ps_requirements(
    settings: Settings,
    incidents: pd.DataFrame,
    target_avg_rt: float = 8.0,
    max_extra_frvs: int = 10,
) -> pd.DataFrame:
    """Estimate required FRVs per police station using simulation similar to district-level.

    Returns a DataFrame with columns: ps, current_frvs, required_frvs
    """
    frv_df, district_counts, frv_by_district = load_frv_allocations(settings)

    results = []
    ps_list = incidents["ps"].dropna().unique()
    for ps in sorted(ps_list):
        ps_mask = incidents[incidents["ps"] == ps]
        # current frvs at PS level: approximate by counting FRV base locations matching ps
        current = int((frv_df[frv_df["UnitDistrict"] == ps].shape[0])) if "UnitDistrict" in frv_df.columns else 0
        if len(ps_mask) < settings.min_cluster_size:
            results.append({"ps": ps, "current_frvs": current, "required_frvs": current})
            continue

        grid = _build_grid_from_district(ps_mask)

        required = current
        for total in range(max(1, current), current + max_extra_frvs + 1):
            labels, adaptive_min_size, n_clusters = run_adaptive_hdbscan(grid, total, settings)
            grid["local_label"] = labels
            local_clusters = sorted(set(labels) - {-1})
            local_to_global = {label: i for i, label in enumerate(local_clusters)}

            cluster_sizes = {}
            cluster_data = {}
            for local_label, global_label in local_to_global.items():
                cluster_grid = grid[grid["local_label"] == local_label]
                points = cluster_grid[["latitude", "longitude"]].to_numpy()
                weights = cluster_grid["weight"].to_numpy()
                cluster_sizes[global_label] = int(weights.sum())
                cluster_data[global_label] = (points, weights)

            allocation = allocate_frvs_to_clusters(cluster_sizes, total)
            medoids: Dict[int, dict] = {}
            medoid_zone_data: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
            global_medoid_id = 0
            frv_idx = 0
            for cluster_id, k in allocation.items():
                if k <= 0:
                    continue
                points, weights = cluster_data[cluster_id]
                zones = sub_cluster_hotspot(points, weights, k, settings)
                for zone_idx, zone in enumerate(zones):
                    medoids[global_medoid_id] = {
                        "district": ps_mask["district"].iloc[0] if "district" in ps_mask.columns else "Unknown",
                        "police_station": ps,
                        "hotspot_id": cluster_id,
                        "sub_zone": zone_idx + 1,
                        "sub_zones_total": len(zones),
                        "frv_id": f"SIM-PS-{total}-{frv_idx}",
                        "latitude": float(zone["medoid"][0]),
                        "longitude": float(zone["medoid"][1]),
                        "size": int(zone["size"]),
                        "grid_cells": int(zone["grid_cells"]),
                        "avg_radius_km": float(zone["avg_radius_km"]),
                        "max_radius_km": float(zone["max_radius_km"]),
                    }
                    medoid_zone_data[global_medoid_id] = (zone["points"], zone["weights"])
                    global_medoid_id += 1
                    frv_idx += 1

            medoids = calculate_response_times(medoids, medoid_zone_data, settings)

            times = []
            weights = []
            for m in medoids.values():
                size = float(m.get("size", 0))
                rt = float(m.get("avg_response_time_min", 0.0))
                times.append(rt)
                weights.append(max(1.0, size))
            if not times:
                avg_rt = float("inf")
            else:
                avg_rt = float(np.average(times, weights=weights))

            if avg_rt <= target_avg_rt:
                required = total
                break

        results.append({"ps": ps, "current_frvs": current, "required_frvs": required})

    res_df = pd.DataFrame(results)
    out_path = settings.output_dir / "ps_frv_requirements.csv"
    res_df.to_csv(out_path, index=False)
    logger.info("Wrote PS FRV requirements to %s", out_path)
    return res_df


def propose_ps_transfers(settings: Settings, req_df: pd.DataFrame) -> pd.DataFrame:
    """Greedy proposer for PS-level transfers within each district."""
    # For simplicity, reuse propose_district_transfers logic grouping by ps
    df = req_df.copy()
    df["surplus"] = df["current_frvs"] - df["required_frvs"]
    donors = df[df["surplus"] > 0].copy().sort_values("surplus", ascending=False)
    receivers = df[df["surplus"] < 0].copy()

    transfers = []
    donor_list = donors.to_dict("records")
    for recv in receivers.to_dict("records"):
        need = -int(recv["surplus"])
        for d in donor_list:
            if need <= 0:
                break
            avail = int(d["surplus"])
            if avail <= 0:
                continue
            take = min(avail, need)
            transfers.append((d["ps"], recv["ps"], take))
            d["surplus"] -= take
            need -= take

    transfer_df = pd.DataFrame(transfers, columns=["from_ps", "to_ps", "n_frvs"]).groupby(["from_ps", "to_ps"]).sum().reset_index()
    out_path = settings.output_dir / "ps_transfer_plan.csv"
    transfer_df.to_csv(out_path, index=False)
    logger.info("Wrote PS transfer plan to %s", out_path)
    return transfer_df


def resimulate_ps_transfers(settings: Settings, incidents: pd.DataFrame, transfer_df: pd.DataFrame) -> pd.DataFrame:
    # Reuse logic from district re-simulation but at PS granularity
    req_df = estimate_ps_requirements(settings, incidents)
    req_df = req_df.set_index("ps")
    for row in transfer_df.itertuples(index=False):
        frm = row.from_ps
        to = row.to_ps
        n = int(row.n_frvs)
        if frm in req_df.index:
            req_df.at[frm, "current_frvs"] = max(0, int(req_df.at[frm, "current_frvs"]) - n)
        if to in req_df.index:
            req_df.at[to, "current_frvs"] = int(req_df.at[to, "current_frvs"]) + n

    out_rows = []
    for ps in req_df.index:
        out_rows.append({"ps": ps, "current_frvs_after": int(req_df.at[ps, "current_frvs"]), "required_frvs": int(req_df.at[ps, "required_frvs"])})

    out = pd.DataFrame(out_rows)
    out_path = settings.output_dir / "ps_optimization_results.csv"
    out.to_csv(out_path, index=False)
    logger.info("Wrote PS optimization results to %s", out_path)
    return out


def select_final_frv_locations(settings: Settings, incidents: pd.DataFrame, allocation_df: pd.DataFrame) -> pd.DataFrame:
    """Select final FRV coordinates per PS using sub-clustering medoid logic.

    allocation_df: DataFrame with columns [ps, allocated_frvs]
    Returns DataFrame with frv_id, ps, latitude, longitude
    """
    rows = []
    for row in allocation_df.itertuples(index=False):
        ps = getattr(row, "ps", None) or getattr(row, "district", None)
        k = int(getattr(row, "allocated_frvs", getattr(row, "required_frvs", 0)))
        if k <= 0:
            continue
        subset = incidents[incidents["ps"] == ps] if "ps" in incidents.columns else incidents[incidents["district"] == ps]
        if subset.empty:
            continue
        grid = _build_grid_from_district(subset)
        # use sub-clustering to get k medoids
        labels, adaptive_min_size, n_clusters = run_adaptive_hdbscan(grid, k, settings)
        grid["local_label"] = labels
        local_clusters = sorted(set(labels) - {-1})
        local_to_global = {label: i for i, label in enumerate(local_clusters)}
        for local_label in local_clusters:
            cluster_grid = grid[grid["local_label"] == local_label]
            points = cluster_grid[["latitude", "longitude"]].to_numpy()
            weights = cluster_grid["weight"].to_numpy()
            medoid = compute_medoid(points, weights, settings)
            rows.append({"ps": ps, "latitude": float(medoid[0]), "longitude": float(medoid[1])})

    final_df = pd.DataFrame(rows)
    out_path = settings.output_dir / "final_frv_locations.csv"
    final_df.to_csv(out_path, index=False)
    logger.info("Wrote final FRV locations to %s", out_path)
    return final_df


def _route_distance_and_order(points: List[Tuple[float, float]]) -> Tuple[List[int], float]:
    # Simple nearest-neighbour + 2-opt using haversine distance
    if not points:
        return [], 0.0
    n = len(points)
    coords = points[:]

    def dist(i, j):
        return aerial_distance_km(coords[i][0], coords[i][1], coords[j][0], coords[j][1], Settings())

    # nearest neighbour
    unvisited = set(range(n))
    order = [0]
    unvisited.remove(0)
    while unvisited:
        last = order[-1]
        next_idx = min(unvisited, key=lambda x: dist(last, x))
        order.append(next_idx)
        unvisited.remove(next_idx)

    # 2-opt
    improved = True
    while improved:
        improved = False
        for i in range(1, n - 2):
            for j in range(i + 1, n - 1):
                a, b = order[i - 1], order[i]
                c, d = order[j], order[j + 1]
                if dist(a, b) + dist(c, d) > dist(a, c) + dist(b, d):
                    order[i:j + 1] = reversed(order[i:j + 1])
                    improved = True
    total = 0.0
    for i in range(len(order) - 1):
        total += dist(order[i], order[i + 1])
    return order, total


def generate_patrol_routes(settings: Settings, final_locations: pd.DataFrame, incidents: pd.DataFrame) -> pd.DataFrame:
    """Generate simple patrol routes per final FRV location using nearby incident/grid points as waypoints.

    Returns DataFrame with frv_id, route_order, latitude, longitude, est_distance_km, est_duration_min
    """
    rows = []
    for idx, loc in final_locations.reset_index().iterrows():
        lat = float(loc["latitude"])
        lon = float(loc["longitude"])
        ps = loc.get("ps")
        # pick nearby incident points as waypoints
        subset = incidents.copy()
        if ps and "ps" in incidents.columns:
            subset = incidents[incidents["ps"] == ps]
        if subset.empty:
            continue
        # sample up to 20 waypoints
        pts = subset[["latitude", "longitude"]].drop_duplicates().sample(min(20, len(subset)), random_state=42).to_numpy().tolist()
        # include base point
        pts.insert(0, [lat, lon])
        order, total_km = _route_distance_and_order([(p[0], p[1]) for p in pts])
        est_duration_min = (total_km / settings.frv_avg_speed_kph) * 60
        for seq, i in enumerate(order):
            rows.append({"frv_id": f"FRV-{idx}", "route_order": seq + 1, "latitude": pts[i][0], "longitude": pts[i][1], "est_distance_km": round(total_km, 2), "est_duration_min": round(est_duration_min, 1)})

    out = pd.DataFrame(rows)
    out_path = settings.output_dir / "patrol_routes.csv"
    out.to_csv(out_path, index=False)
    logger.info("Wrote patrol routes to %s", out_path)
    return out


def resimulate_district_transfers(settings: Settings, incidents: pd.DataFrame, transfer_df: pd.DataFrame) -> pd.DataFrame:
    """Apply transfers virtually and re-run district-level sufficiency/simulation to compute before/after metrics.

    Returns a DataFrame summarizing before/after average RT per district.
    """
    # For simplicity, treat transfers as changing the 'current_frvs' used in estimate_frv_requirements
    req_df = estimate_frv_requirements(settings, incidents)
    req_df = req_df.set_index("district")

    for row in transfer_df.itertuples(index=False):
        frm = row.from_district
        to = row.to_district
        n = int(row.n_frvs)
        if frm in req_df.index:
            req_df.at[frm, "current_frvs"] = max(0, int(req_df.at[frm, "current_frvs"]) - n)
        if to in req_df.index:
            req_df.at[to, "current_frvs"] = int(req_df.at[to, "current_frvs"]) + n

    # Re-run simulation for adjusted current_frvs to compute metrics
    # Here we'll compute average RT per district using the required_frvs as placement counts
    summary_rows = []
    for district in req_df.index:
        current = int(req_df.at[district, "current_frvs"]) if pd.notna(req_df.at[district, "current_frvs"]) else 0
        required = int(req_df.at[district, "required_frvs"]) if pd.notna(req_df.at[district, "required_frvs"]) else current
        summary_rows.append({"district": district, "current_frvs_after": current, "required_frvs": required})

    out = pd.DataFrame(summary_rows)
    out_path = settings.output_dir / "district_optimization_results.csv"
    out.to_csv(out_path, index=False)
    logger.info("Wrote district optimization results to %s", out_path)
    return out


def run_full_optimization(settings, incidents, medoids) -> Dict[str, object]:
    logger.info("Starting optimization: FRV sufficiency and district transfers")

    # Stage 6: FRV sufficiency
    req_df = estimate_frv_requirements(settings, incidents)

    # Stage 7: District-level optimization (greedy proposer)
    transfer_df = propose_district_transfers(req_df)
    transfer_path = settings.output_dir / "district_transfer_plan.csv"
    transfer_df.to_csv(transfer_path, index=False)

    # Stage 8: District re-simulation
    district_results = resimulate_district_transfers(settings, incidents, transfer_df)
    # Stage 9: PS-level optimization
    ps_req_df = estimate_ps_requirements(settings, incidents)
    ps_transfer_df = propose_ps_transfers(settings, ps_req_df)

    # Stage 10: PS re-simulation
    ps_results = resimulate_ps_transfers(settings, incidents, ps_transfer_df)

    # Stage 11: Final FRV placement
    # Use required_frvs as allocation for final placement
    allocation_df = ps_req_df.rename(columns={"required_frvs": "allocated_frvs"})[["ps", "allocated_frvs"]]
    final_locations = select_final_frv_locations(settings, incidents, allocation_df)

    # Stage 13-14: Patrol zone generation & route optimization (simple NN+2opt)
    from .patrol_routing import generate_patrol_routes
    patrol_routes = generate_patrol_routes(settings, final_locations, incidents)

    # Stage 12: Final system simulation (lightweight using aerial+road factor)
    # For each final location, compute avg/95/max times using nearest incident points
    samples = []
    for idx, loc in final_locations.reset_index().iterrows():
        lat = float(loc["latitude"])
        lon = float(loc["longitude"])
        ps = loc.get("ps")
        subset = incidents[incidents["ps"] == ps] if ps and "ps" in incidents.columns else incidents
        if subset.empty:
            continue
        # sample up to 10 nearest incidents
        subset = subset.copy()
        subset["dist_km"] = subset.apply(lambda r: aerial_distance_km(lat, lon, float(r["latitude"]), float(r["longitude"]), settings), axis=1)
        nearest = subset.nsmallest(10, "dist_km")
        road_dists = nearest["dist_km"].values * settings.road_factor
        times_min = (road_dists / settings.frv_avg_speed_kph) * 60
        for t in times_min:
            samples.append(float(t))

    if samples:
        final_avg = float(np.mean(samples))
        final_median = float(np.median(samples))
        final_95 = float(np.percentile(samples, 95))
        final_max = float(np.max(samples))
        coverage = float(len(samples) / max(1, len(incidents))) * 100.0
    else:
        final_avg = final_median = final_95 = final_max = 0.0
        coverage = 0.0

    final_metrics = {
        "final_avg_response_time_min": final_avg,
        "final_median_response_time_min": final_median,
        "final_95th_response_time_min": final_95,
        "final_max_response_time_min": final_max,
        "final_coverage_percent": coverage,
    }
    final_path = settings.output_dir / "final_system_metrics.json"
    import json

    final_path.write_text(json.dumps(final_metrics, indent=2), encoding="utf-8")
    logger.info("Wrote final system metrics to %s", final_path)

    result = {
        "frv_requirements": str(settings.output_dir / "frv_requirements.csv"),
        "district_transfer_plan": str(transfer_path),
        "district_optimization_results": str(settings.output_dir / "district_optimization_results.csv"),
        "ps_frv_requirements": str(settings.output_dir / "ps_frv_requirements.csv"),
        "ps_transfer_plan": str(settings.output_dir / "ps_transfer_plan.csv"),
        "ps_optimization_results": str(settings.output_dir / "ps_optimization_results.csv"),
        "final_frv_locations": str(settings.output_dir / "final_frv_locations.csv"),
        "patrol_routes_csv": str(settings.output_dir / "patrol_routes.csv"),
        "final_system_metrics": str(final_path),
        "frv_requirements_df": req_df,
        "transfer_df": transfer_df,
        "district_results_df": district_results,
        "ps_requirements_df": ps_req_df,
        "ps_transfer_df": ps_transfer_df,
        "ps_results_df": ps_results,
        "final_locations_df": final_locations,
        "patrol_routes_df": patrol_routes,
    }

    logger.info("Optimization complete (proposals only). No deployments were modified.")
    return result
