from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics.pairwise import haversine_distances

from .config import Settings
from .frv import load_frv_allocations, normalize_name
from .response_time import calculate_response_times

logger = logging.getLogger(__name__)


def compute_medoid(points: np.ndarray, weights: np.ndarray | None = None, settings: Settings | None = None) -> np.ndarray:
    if len(points) <= 1:
        return points[0]

    max_points = 2000
    sample_points = points
    sample_weights = weights
    if len(points) > max_points:
        rng = np.random.default_rng(42)
        probabilities = weights / weights.sum() if weights is not None else None
        idx = rng.choice(len(points), max_points, replace=False, p=probabilities)
        sample_points = points[idx]
        sample_weights = weights[idx] if weights is not None else None

    earth_radius = settings.earth_radius_km if settings else 6371.0
    dist_matrix = haversine_distances(np.radians(sample_points)) * earth_radius
    total_distances = (dist_matrix * sample_weights).sum(axis=1) if sample_weights is not None else dist_matrix.sum(axis=1)
    return sample_points[int(np.argmin(total_distances))]


def compute_radius(medoid: np.ndarray, points: np.ndarray, weights: np.ndarray, settings: Settings) -> tuple[float, float]:
    distances = haversine_distances(np.radians(medoid.reshape(1, -1)), np.radians(points))[0]
    road_distances = distances * settings.earth_radius_km * settings.road_factor
    return float(np.average(road_distances, weights=weights)), float(road_distances.max())


def allocate_frvs_to_clusters(cluster_sizes: dict[int, int], total_frvs: int) -> dict[int, int]:
    if not cluster_sizes:
        return {}

    if total_frvs >= len(cluster_sizes):
        allocation = {cid: 1 for cid in cluster_sizes}
        remaining = total_frvs - len(cluster_sizes)
        if remaining <= 0:
            return allocation

        total_incidents = sum(cluster_sizes.values())
        sorted_clusters = sorted(cluster_sizes.items(), key=lambda item: -item[1])
        for cid, size in sorted_clusters:
            allocation[cid] += int(round(remaining * size / total_incidents))

        diff = total_frvs - sum(allocation.values())
        keys = [cid for cid, _ in sorted_clusters]
        i = 0
        while diff != 0 and keys:
            cid = keys[i % len(keys)]
            if diff > 0:
                allocation[cid] += 1
                diff -= 1
            elif allocation[cid] > 1:
                allocation[cid] -= 1
                diff += 1
            i += 1
        return allocation

    sorted_clusters = sorted(cluster_sizes.items(), key=lambda item: -item[1])
    return {cid: 1 if i < total_frvs else 0 for i, (cid, _) in enumerate(sorted_clusters)}


def sub_cluster_hotspot(points: np.ndarray, weights: np.ndarray, k: int, settings: Settings) -> list[dict[str, object]]:
    k = min(max(k, 1), len(points))
    if k == 1 or len(points) <= 1:
        medoid = compute_medoid(points, weights, settings)
        avg_radius, max_radius = compute_radius(medoid, points, weights, settings)
        return [_zone_payload(medoid, points, weights, avg_radius, max_radius)]

    labels = KMeans(n_clusters=k, random_state=42, n_init=10).fit_predict(points, sample_weight=weights)
    zones = []
    for label in range(k):
        mask = labels == label
        if not mask.any():
            continue
        sub_points = points[mask]
        sub_weights = weights[mask]
        medoid = compute_medoid(sub_points, sub_weights, settings)
        avg_radius, max_radius = compute_radius(medoid, sub_points, sub_weights, settings)
        zones.append(_zone_payload(medoid, sub_points, sub_weights, avg_radius, max_radius))
    return zones


def _zone_payload(medoid: np.ndarray, points: np.ndarray, weights: np.ndarray, avg_radius: float, max_radius: float) -> dict[str, object]:
    return {
        "medoid": medoid,
        "points": points,
        "weights": weights,
        "size": int(weights.sum()),
        "grid_cells": len(points),
        "avg_radius_km": avg_radius,
        "max_radius_km": max_radius,
    }


def run_adaptive_hdbscan(
    grid_df: pd.DataFrame,
    n_frvs: int | None,
    settings: Settings,
) -> tuple[np.ndarray, int, int]:
    import hdbscan

    if len(grid_df) < 2 or n_frvs == 0:
        return np.full(len(grid_df), -1), settings.min_cluster_size, 0
    if len(grid_df) < settings.min_cluster_size:
        return np.zeros(len(grid_df), dtype=int), settings.min_cluster_size, 1

    adaptive_min_size = settings.min_cluster_size
    while True:
        expanded_coords: list[list[float]] = []
        repeats_per_row: list[int] = []
        for row in grid_df.itertuples():
            repeats = min(int(row.weight), adaptive_min_size)
            expanded_coords.extend([[row.latitude, row.longitude]] * repeats)
            repeats_per_row.append(repeats)

        coords_rad = np.radians(np.array(expanded_coords))
        if len(coords_rad) < adaptive_min_size:
            return np.zeros(len(grid_df), dtype=int), adaptive_min_size, 1

        labels = hdbscan.HDBSCAN(
            min_cluster_size=adaptive_min_size,
            min_samples=min(settings.min_samples, max(len(grid_df) - 1, 1)),
            metric="haversine",
            cluster_selection_method="eom",
            core_dist_n_jobs=-1,
        ).fit_predict(coords_rad)

        collapsed = []
        idx = 0
        for repeats in repeats_per_row:
            collapsed.append(labels[idx])
            idx += repeats

        grid_labels = np.array(collapsed)
        local_clusters = sorted(set(grid_labels) - {-1})

        if n_frvs is None:
            return grid_labels, adaptive_min_size, len(local_clusters)

        if len(local_clusters) <= n_frvs or len(local_clusters) <= 1:
            return grid_labels, adaptive_min_size, len(local_clusters)

        cluster_weights = [int(grid_df.loc[grid_labels == label, "weight"].sum()) for label in local_clusters]
        cluster_weights.sort(reverse=True)
        next_threshold = cluster_weights[n_frvs] + 1
        adaptive_min_size = next_threshold if next_threshold > adaptive_min_size else adaptive_min_size + 5
        if adaptive_min_size > len(coords_rad):
            return grid_labels, adaptive_min_size, len(local_clusters)


def run_district_level_clustering(df: pd.DataFrame, settings: Settings) -> tuple[pd.DataFrame, dict[int, dict], dict[str, dict]]:
    frv_df, frv_counts, frv_by_district = load_frv_allocations(settings)
    df = df.copy()
    df["cluster_label"] = -1

    global_cluster_id = 0
    global_medoid_id = 0
    medoids: dict[int, dict] = {}
    medoid_zone_data: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    district_summaries: dict[str, dict] = {}

    for district in sorted(d for d in df["district"].dropna().unique() if d != "Outside MP"):
        dist_mask = df["district"] == district
        dist_df = df[dist_mask].copy()
        n_frvs = frv_counts.get(district, 0)
        frv_ids = frv_by_district.get(district, [])

        if len(dist_df) < 10 or n_frvs == 0:
            district_summaries[district] = _district_summary(len(dist_df), n_frvs, 0, len(dist_df), skipped=True)
            continue

        dist_df["lat_grid"] = dist_df["latitude"].round(3)
        dist_df["lon_grid"] = dist_df["longitude"].round(3)
        dist_df["grid_key"] = dist_df["lat_grid"].astype(str) + "_" + dist_df["lon_grid"].astype(str)
        grid = dist_df.groupby("grid_key").agg(
            latitude=("latitude", "mean"),
            longitude=("longitude", "mean"),
            weight=("latitude", "size"),
        ).reset_index()

        grid_labels, adaptive_min_size, n_clusters = run_adaptive_hdbscan(grid, n_frvs, settings)
        grid["local_label"] = grid_labels
        local_clusters = sorted(set(grid_labels) - {-1})
        local_to_global = {label: global_cluster_id + i for i, label in enumerate(local_clusters)}
        global_cluster_id += len(local_clusters)
        grid["global_label"] = grid["local_label"].map(lambda label: local_to_global.get(label, -1))

        key_to_label = dict(zip(grid["grid_key"], grid["global_label"]))
        temp_key = df.loc[dist_mask, "latitude"].round(3).astype(str) + "_" + df.loc[dist_mask, "longitude"].round(3).astype(str)
        df.loc[dist_mask, "cluster_label"] = temp_key.map(key_to_label).fillna(-1).astype(int)
        dist_df = df.loc[dist_mask].copy()
        cluster_sizes: dict[int, int] = {}
        cluster_data: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        for local_label, global_label in local_to_global.items():
            cluster_grid = grid[grid["local_label"] == local_label]
            points = cluster_grid[["latitude", "longitude"]].to_numpy()
            weights = cluster_grid["weight"].to_numpy()
            cluster_sizes[global_label] = int(weights.sum())
            cluster_data[global_label] = (points, weights)

        allocation = allocate_frvs_to_clusters(cluster_sizes, n_frvs)
        frv_idx = 0
        print("DIST_DF COLUMNS:")
        print(dist_df.columns.tolist())
        cluster_ps = {}

        for cluster_id in cluster_data.keys():


            cluster_incidents = dist_df[
                dist_df["cluster_label"] == cluster_id
            ]

            if len(cluster_incidents) > 0:
                cluster_ps[cluster_id] = (
                    cluster_incidents["ps"]
                    .mode()
                    .iloc[0]
                )
            else:
                cluster_ps[cluster_id] = "Unknown"
        for cluster_id, k in allocation.items():
            if k <= 0:
                continue
            points, weights = cluster_data[cluster_id]
            info_ps = cluster_ps.get(
                cluster_id,
                "Unknown"
            )
            zones = sub_cluster_hotspot(points, weights, k, settings)
            for zone_idx, zone in enumerate(zones):
                frv_id = frv_ids[frv_idx] if frv_idx < len(frv_ids) else f"UNASSIGNED-{frv_idx}"
                frv_idx += 1
                medoids[global_medoid_id] = {
                    "district": district,
                    "police_station": info_ps,
                    "hotspot_id": cluster_id,
                    "sub_zone": zone_idx + 1,
                    "sub_zones_total": len(zones),
                    "frv_id": frv_id,
                    "latitude": float(zone["medoid"][0]),
                    "longitude": float(zone["medoid"][1]),
                    "size": int(zone["size"]),
                    "grid_cells": int(zone["grid_cells"]),
                    "avg_radius_km": round(float(zone["avg_radius_km"]), 2),
                    "max_radius_km": round(float(zone["max_radius_km"]), 2),
                }
                medoid_zone_data[global_medoid_id] = (zone["points"], zone["weights"])
                global_medoid_id += 1

        while frv_idx < len(frv_ids):
            medoids[global_medoid_id] = {
                "district": district,
                "police_station": "Unassigned",
                "hotspot_id": -1,
                "sub_zone": 0,
                "sub_zones_total": 0,
                "frv_id": frv_ids[frv_idx],
                "latitude": float(dist_df["latitude"].mean()),
                "longitude": float(dist_df["longitude"].mean()),
                "size": 0,
                "grid_cells": 0,
                "avg_radius_km": 0.0,
                "max_radius_km": 0.0,
            }
            frv_idx += 1
            global_medoid_id += 1

        noise_count = int((df.loc[dist_mask, "cluster_label"] == -1).sum())
        district_summaries[district] = _district_summary(
            len(dist_df), n_frvs, n_clusters, noise_count, skipped=False, adaptive_min_size=adaptive_min_size
        )
        logger.info("%s: %s incidents, %s FRVs, %s hotspots, %s noise", district, len(dist_df), n_frvs, n_clusters, noise_count)

    medoids = calculate_response_times(medoids, medoid_zone_data, settings)
    medoids = attach_nearest_station_distances(medoids, frv_df, settings)
    write_reports(df, medoids, district_summaries, settings)
    return df, medoids, district_summaries


def _district_summary(total: int, frvs: int, clusters: int, noise: int, skipped: bool, adaptive_min_size: int | None = None) -> dict:
    return {
        "total_incidents": total,
        "n_clusters": clusters,
        "n_frvs": frvs,
        "n_frvs_placed": frvs if not skipped else 0,
        "n_clustered": total - noise,
        "n_noise": noise,
        "noise_pct": round(noise / max(total, 1) * 100, 1),
        "adaptive_min_size": adaptive_min_size,
        "police_stations": {},
        "skipped": skipped,
    }


def attach_nearest_station_distances(medoids: dict[int, dict], frv_df: pd.DataFrame, settings: Settings) -> dict[int, dict]:
    ref = frv_df.copy()
    ref["base_lat"] = pd.to_numeric(ref.get("base_lat"), errors="coerce")
    ref["base_lon"] = pd.to_numeric(ref.get("base_lon"), errors="coerce")
    ref["ps_lower"] = ref["UnitPoliceStation"].apply(normalize_name)
    ref = ref.dropna(subset=["district", "ps_lower", "base_lat", "base_lon"])
    ref = ref[(ref["ps_lower"] != "") & (ref["base_lat"] != 0) & (ref["base_lon"] != 0)]
    if ref.empty:
        return medoids

    stations = ref.groupby(["district", "ps_lower"], as_index=False).agg(
        police_station=("UnitPoliceStation", "first"),
        base_location=("UnitBaseLocation", "first"),
        latitude=("base_lat", "mean"),
        longitude=("base_lon", "mean"),
    )

    for info in medoids.values():
        nearest = _nearest_station(info, stations, settings)
        distance = nearest.get("distance_km") if nearest else None
        info.update(
            {
                "nearest_police_station": nearest.get("police_station") if nearest else None,
                "nearest_base_location": nearest.get("base_location") if nearest else None,
                "nearest_ps_latitude": round(float(nearest["latitude"]), 6) if nearest else None,
                "nearest_ps_longitude": round(float(nearest["longitude"]), 6) if nearest else None,
                "distance_to_nearest_ps_km": None if distance is None else round(distance, 2),
                "road_factor_distance_to_nearest_ps_km": None if distance is None else round(distance * settings.road_factor, 2),
                "ps_location_source": "Master_Unit_Export.csv UnitBaseLocation_X/Y",
            }
        )
    return medoids


def _nearest_station(info: dict, stations: pd.DataFrame, settings: Settings) -> dict | None:
    candidates = stations[stations["district"] == info.get("district")]
    if candidates.empty:
        return None
    lat = float(info["latitude"])
    lon = float(info["longitude"])
    dlat = np.radians(candidates["latitude"].to_numpy(float) - lat)
    dlon = np.radians(candidates["longitude"].to_numpy(float) - lon)
    lat1 = np.radians(lat)
    lat2 = np.radians(candidates["latitude"].to_numpy(float))
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    distances = settings.earth_radius_km * 2 * np.arcsin(np.sqrt(a))
    idx = int(np.argmin(distances))
    nearest = candidates.iloc[idx].to_dict()
    nearest["distance_km"] = float(distances[idx])
    return nearest


def write_reports(df: pd.DataFrame, medoids: dict[int, dict], district_summaries: dict[str, dict], settings: Settings) -> None:
    settings.ensure_dirs()
    df.to_csv(settings.clustered_csv, index=False)
    settings.medoids_json.write_text(json.dumps({str(k): v for k, v in medoids.items()}, indent=2), encoding="utf-8")
    settings.district_summaries_json.write_text(json.dumps(district_summaries, indent=2), encoding="utf-8")

    placement_rows = []
    response_rows = []
    for medoid_id, info in medoids.items():
        placement_rows.append(
            {
                "medoid_id": medoid_id,
                "frv_id": info.get("frv_id"),
                "district": info.get("district"),
                "placement_latitude": info.get("latitude"),
                "placement_longitude": info.get("longitude"),
                "nearest_police_station": info.get("nearest_police_station"),
                "nearest_base_location": info.get("nearest_base_location"),
                "nearest_ps_latitude": info.get("nearest_ps_latitude"),
                "nearest_ps_longitude": info.get("nearest_ps_longitude"),
                "distance_to_nearest_ps_km": info.get("distance_to_nearest_ps_km"),
                "road_factor_distance_to_nearest_ps_km": info.get("road_factor_distance_to_nearest_ps_km"),
                "hotspot_id": info.get("hotspot_id"),
                "sub_zone": info.get("sub_zone"),
                "sub_zones_total": info.get("sub_zones_total"),
                "incidents_covered": info.get("size"),
                "avg_radius_km": info.get("avg_radius_km"),
                "max_radius_km": info.get("max_radius_km"),
                "avg_response_time_min": info.get("avg_response_time_min"),
                "max_response_time_min": info.get("max_response_time_min"),
                "ps_location_source": info.get("ps_location_source"),
            }
        )
        response_rows.append(
            {
                "medoid_id": medoid_id,
                "frv_id": info.get("frv_id"),
                "district": info.get("district"),
                "police_station": info.get("police_station"),
                "avg_response_time_min": info.get("avg_response_time_min"),
                "max_response_time_min": info.get("max_response_time_min"),
                "source": info.get("response_time_source"),
                "incidents_covered": info.get("size"),
                "latitude": info.get("latitude"),
                "longitude": info.get("longitude"),
            }
        )
    pd.DataFrame(placement_rows).to_csv(settings.frv_placement_csv, index=False)
    pd.DataFrame(response_rows).to_csv(settings.response_summary_csv, index=False)
