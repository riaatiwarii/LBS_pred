from __future__ import annotations

import logging
import time

import numpy as np
import requests
from sklearn.metrics.pairwise import haversine_distances

from .config import Settings

logger = logging.getLogger(__name__)


def sample_zone_points(points: np.ndarray, weights: np.ndarray, n_samples: int) -> list[list[float]]:
    if len(points) <= n_samples:
        return points.tolist()
    centroid = np.average(points, weights=weights, axis=0)
    distances = np.sqrt(((points - centroid) ** 2).sum(axis=1))
    indices = {int(np.argmax(distances)), int(np.argmin(distances))}
    sorted_idx = np.argsort(distances)
    step = max(1, len(sorted_idx) // max(n_samples - len(indices) + 1, 1))
    for i in range(0, len(sorted_idx), step):
        indices.add(int(sorted_idx[i]))
        if len(indices) >= n_samples:
            break
    return points[list(indices)].tolist()


def calculate_response_times(
    medoids: dict[int, dict],
    zone_data: dict[int, tuple[np.ndarray, np.ndarray]],
    settings: Settings,
) -> dict[int, dict]:
    session = requests.Session()
    cache: dict[tuple[float, float, tuple[tuple[float, float], ...]], list[tuple[float | None, float | None]]] = {}
    road_success = 0

    for index, (medoid_id, info) in enumerate(medoids.items(), 1):
        if info.get("avg_radius_km", 0) == 0 and info.get("max_radius_km", 0) == 0:
            info.update(avg_response_time_min=0.0, max_response_time_min=0.0, response_time_source="road", sample_points=[])
            continue

        sample_points = []
        if medoid_id in zone_data:
            points, weights = zone_data[medoid_id]
            sample_points = sample_zone_points(points, weights, settings.response_time_sample_points)
        info["sample_points"] = sample_points
        info["sample_points_details"] = []
        info["route_geometries"] = []

        road_results = []
        if sample_points:
            road_results = osrm_table_times(float(info["latitude"]), float(info["longitude"]), sample_points, session, cache, settings)

        if road_results and any(duration is not None for duration, _ in road_results):
            times = []
            for point, (duration_min, distance_km) in zip(sample_points, road_results):
                fallback_distance, fallback_time = fallback_for_point(info, point, settings)
                clean_time = duration_min
                clean_distance = distance_km
                if duration_min is None or distance_km is None or is_routing_anomaly(info, point, distance_km, settings):
                    clean_time = fallback_time
                    clean_distance = fallback_distance
                times.append(float(clean_time))
                info["sample_points_details"].append(
                    {
                        "latitude": float(point[0]),
                        "longitude": float(point[1]),
                        "distance_km": round(float(clean_distance), 2),
                        "time_min": round(float(clean_time), 1),
                    }
                )
            info["avg_response_time_min"] = round(float(np.mean(times)), 1)
            info["max_response_time_min"] = round(float(max(times)), 1)
            info["response_time_source"] = "road"
            road_success += 1
            time.sleep(settings.osrm_rate_limit_seconds)
        else:
            avg_time, max_time = aerial_zone_fallback(info, settings)
            info["avg_response_time_min"] = avg_time
            info["max_response_time_min"] = max_time
            info["response_time_source"] = "aerial"

        if index % 100 == 0 or index == len(medoids):
            logger.info("Response times %s/%s, road-based %s", index, len(medoids), road_success)

    return medoids


def osrm_table_times(
    origin_lat: float,
    origin_lon: float,
    dest_points: list[list[float]],
    session: requests.Session,
    cache: dict,
    settings: Settings,
) -> list[tuple[float | None, float | None]]:
    key = (
        round(origin_lat, 6),
        round(origin_lon, 6),
        tuple((round(float(lat), 6), round(float(lon), 6)) for lat, lon in dest_points),
    )
    if key in cache:
        return cache[key]

    coords = f"{origin_lon},{origin_lat};" + ";".join(f"{lon},{lat}" for lat, lon in dest_points)
    try:
        if settings.mapbox_access_token:
            url = (
                f"https://api.mapbox.com/directions-matrix/v1/mapbox/driving/{coords}"
                f"?sources=0&annotations=duration,distance&access_token={settings.mapbox_access_token}"
            )
        else:
            url = f"{settings.osrm_base_url}/table/v1/driving/{coords}?sources=0&annotations=duration,distance"
        response = session.get(url, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "Ok":
            raise ValueError(payload.get("code"))
        durations = payload["durations"][0][1:]
        distances = payload["distances"][0][1:]
        results = []
        for duration, distance in zip(durations, distances):
            if duration is None or distance is None:
                results.append((None, None))
                continue
            distance_km = distance / 1000.0
            if settings.use_osrm_driving_time:
                time_min = duration / 60.0
            else:
                time_min = (distance_km / settings.frv_avg_speed_kph) * 60
            results.append((time_min, distance_km))
        cache[key] = results
        return results
    except Exception as exc:
        logger.debug("OSRM table failed: %s", exc)
        results = [(None, None)] * len(dest_points)
        cache[key] = results
        return results


def is_routing_anomaly(info: dict, point: list[float], road_distance_km: float, settings: Settings) -> bool:
    aerial = aerial_distance_km(float(info["latitude"]), float(info["longitude"]), float(point[0]), float(point[1]), settings)
    return aerial > 0 and road_distance_km > 3.5 * aerial


def fallback_for_point(info: dict, point: list[float], settings: Settings) -> tuple[float, float]:
    aerial = aerial_distance_km(float(info["latitude"]), float(info["longitude"]), float(point[0]), float(point[1]), settings)
    road_distance = aerial * settings.road_factor
    return road_distance, (road_distance / settings.frv_avg_speed_kph) * 60


def aerial_zone_fallback(info: dict, settings: Settings) -> tuple[float, float]:
    avg_time = info.get("avg_radius_km", 0) / settings.frv_avg_speed_kph * 60
    max_time = info.get("max_radius_km", 0) / settings.frv_avg_speed_kph * 60
    return round(float(avg_time), 1), round(float(max_time), 1)


def aerial_distance_km(lat1: float, lon1: float, lat2: float, lon2: float, settings: Settings) -> float:
    return float(
        haversine_distances(np.radians([[lat1, lon1]]), np.radians([[lat2, lon2]]))[0, 0]
        * settings.earth_radius_km
    )
