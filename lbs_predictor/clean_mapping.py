from __future__ import annotations

import json
import logging
from html import escape
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from .config import Settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def generate_clean_map(settings: Settings) -> str:
    """Build map_data.json consumed by the standalone web/ map."""
    medoids   = _load_json(settings.medoids_json)
    summaries = _load_json(settings.district_summaries_json)

    district_geojson, district_bounds = _load_district_boundaries(settings, summaries)
    ps_geojson, ps_bounds, district_ps_map, ps_lookup, ps_points = _load_ps_boundaries(settings)
    frv_points = _build_deployment_points(medoids, ps_lookup)
    _attach_frv_counts(ps_points, frv_points)
    ps_point_lookup = {point["psKey"]: point for point in ps_points}
    transfer_rows = _load_transfer_rows(settings)
    resimulation_rows = _load_resimulation_rows(settings)
    patrol_routes = _load_patrol_routes(settings, ps_lookup)
    optimized_frv_points = _build_optimized_deployment_points(
        frv_points,
        transfer_rows,
        resimulation_rows,
        ps_point_lookup,
    )
    _attach_patrol_metrics(frv_points, patrol_routes)
    _attach_patrol_metrics(optimized_frv_points, patrol_routes)

    center_lat, center_lon = _map_center(frv_points, settings)
    all_bounds = _combined_bounds(district_bounds.values())

    payload = {
        "districtGeojson": district_geojson,
        "psGeojson":        ps_geojson,
        "districtBounds":   district_bounds,
        "psBounds":         ps_bounds,
        "districtPsMap":    district_ps_map,
        "frvPoints":        frv_points,
        "currentFrvPoints": frv_points,
        "optimizedFrvPoints": optimized_frv_points,
        "transferRows":     transfer_rows,
        "resimulationRows": resimulation_rows,
        "patrolRoutes":     patrol_routes,
        "psPoints":         ps_points,
        "allBounds":        all_bounds,
        "defaultCenter":    [center_lat, center_lon],
        "defaultZoom":      settings.map_zoom,
    }

    output_path = _data_output_path(settings.map_html)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
    output_path.write_text(payload_text, encoding="utf-8")

    web_output_path = settings.project_root / "lbs_predictor" / "web" / "map_data.json"
    web_output_path.parent.mkdir(parents=True, exist_ok=True)
    web_output_path.write_text(payload_text, encoding="utf-8")

    logger.info("Map data saved to %s", output_path)
    logger.info("Web map data saved to %s", web_output_path)
    return str(output_path)


def generate_map(settings: Settings) -> str:
    """Compatibility wrapper."""
    return generate_clean_map(settings)


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_district_boundaries(settings: Settings, summaries: dict) -> tuple[dict, dict]:
    if not settings.districts_geojson.exists():
        return {"type": "FeatureCollection", "features": []}, {}

    districts = gpd.read_file(settings.districts_geojson).to_crs(epsg=4326)
    name_col = "dst_nme" if "dst_nme" in districts.columns else "dtname"
    districts["map_district"] = districts[name_col].astype(str).str.strip()
    districts["incidents"] = districts["map_district"].apply(
        lambda name: summaries.get(name, {}).get("total_incidents", 0)
    )
    districts["frvs"] = districts["map_district"].apply(
        lambda name: summaries.get(name, {}).get("n_frvs", 0)
    )
    districts["avg_resp"] = districts["map_district"].apply(
        lambda name: summaries.get(name, {}).get("avg_response_time_min", 0)
    )
    bounds  = {row.map_district: _geometry_bounds(row.geometry) for row in districts.itertuples()}
    geojson = json.loads(
        districts[["map_district", "incidents", "frvs", "avg_resp", "geometry"]].to_json()
    )
    return geojson, bounds


def _load_ps_boundaries(settings: Settings) -> tuple[dict, dict, dict, list[dict], list[dict]]:
    if not settings.police_station_geojson.exists():
        return {"type": "FeatureCollection", "features": []}, {}, {}, [], []

    police_stations = gpd.read_file(settings.police_station_geojson).to_crs(epsg=4326)
    police_stations["map_district"] = police_stations["dst_nme"].astype(str).str.strip()
    police_stations["map_ps"]       = police_stations["ps"].astype(str).str.strip()
    police_stations["map_ps_key"]   = (
        police_stations["map_district"] + "||" + police_stations["map_ps"]
    )

    bounds:        dict[str, list]  = {}
    district_ps_map: dict[str, list] = {}
    ps_lookup:     list[dict]       = []
    ps_points:     list[dict]       = []

    for row in police_stations.itertuples():
        if not row.map_district or not row.map_ps:
            continue
        bounds[row.map_ps_key] = _geometry_bounds(row.geometry)
        district_ps_map.setdefault(row.map_district, []).append(
            {"label": row.map_ps, "value": row.map_ps_key}
        )
        point = row.geometry.representative_point()
        ps_lookup.append({
            "district": row.map_district,
            "ps":       row.map_ps,
            "psKey":    row.map_ps_key,
            "geometry": row.geometry,
        })
        ps_points.append({
            "lat":             float(point.y),
            "lon":             float(point.x),
            "district":        row.map_district,
            "ps":              row.map_ps,
            "psKey":           row.map_ps_key,
            "frvCount":        0,
            "nearestDistance": 0.0,
            "avgDistance":     0.0,
        })

    for items in district_ps_map.values():
        items.sort(key=lambda item: item["label"])

    geojson = json.loads(
        police_stations[["map_district", "map_ps", "map_ps_key", "geometry"]].to_json()
    )
    return geojson, bounds, district_ps_map, ps_lookup, ps_points


def _load_transfer_rows(settings: Settings) -> list[dict]:
    path = settings.output_dir / "04_redistribution_transfers.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path).fillna("")
    return [_json_safe_row(row) for row in df.to_dict("records")]


def _load_resimulation_rows(settings: Settings) -> list[dict]:
    path = settings.output_dir / "05_resimulation_results.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path).fillna("")
    return [_json_safe_row(row) for row in df.to_dict("records")]


def _load_patrol_routes(settings: Settings, ps_lookup: list[dict]) -> list[dict]:
    path = settings.output_dir / "patrol_routes.csv"
    if not path.exists():
        return []

    df = pd.read_csv(path).fillna("")
    if df.empty:
        return []

    ps_to_district = {}
    for item in ps_lookup:
        ps_to_district.setdefault(item["ps"], item["district"])

    routes: list[dict] = []
    sort_cols = [col for col in ("route_id", "stop_seq") if col in df.columns]
    if sort_cols:
        df = df.sort_values(sort_cols)

    for route_id, route_df in df.groupby("route_id", sort=False):
        route_df = route_df.sort_values("stop_seq") if "stop_seq" in route_df.columns else route_df
        points = []
        waypoint_count = 0
        for row in route_df.to_dict("records"):
            lat = _safe_float(row.get("latitude"))
            lon = _safe_float(row.get("longitude"))
            if lat is None or lon is None:
                continue
            stop_type = str(row.get("stop_type") or "")
            if stop_type.upper() == "WAYPOINT":
                waypoint_count += 1
            points.append({
                "lat": lat,
                "lon": lon,
                "stopSeq": _safe_int(row.get("stop_seq")),
                "stopType": stop_type,
                "incidentWeight": _safe_float(row.get("incident_weight")) or 0,
                "cumulativeDistKm": _safe_float(row.get("cumulative_dist_km")) or 0,
            })

        if len(points) < 2:
            continue

        first = route_df.iloc[0].to_dict()
        ps = str(first.get("ps") or "").strip()
        route = {
            "routeId": str(route_id),
            "frvId": str(first.get("frv_id") or ""),
            "district": ps_to_district.get(ps, ""),
            "ps": ps,
            "points": points,
            "routeLengthKm": _safe_float(first.get("total_route_km")) or 0,
            "durationMin": _safe_float(first.get("est_duration_min")) or 0,
            "coverage": _safe_float(first.get("coverage_pct")) or 0,
            "waypointCount": waypoint_count,
            "valid": _safe_bool(first.get("valid")),
        }
        routes.append(route)

    return routes


# ---------------------------------------------------------------------------
# Deployment / FRV helpers
# ---------------------------------------------------------------------------

def _build_deployment_points(medoids: dict, ps_lookup: list[dict]) -> list[dict]:
    frv_points: list[dict] = []

    for info in medoids.values():
        lat = info.get("latitude")
        lon = info.get("longitude")
        if lat is None or lon is None:
            continue

        point      = Point(float(lon), float(lat))
        matched_ps = _match_ps_for_point(point, str(info.get("district") or "").strip(), ps_lookup)
        district   = matched_ps["district"] if matched_ps else str(info.get("district") or "").strip()
        ps_name    = matched_ps["ps"]       if matched_ps else _deployment_ps_name(info)
        ps_key     = matched_ps["psKey"]    if matched_ps else (
            f"{district}||{ps_name}" if district and ps_name else ""
        )
        avg_response = float(info.get("avg_response_time_min") or 0)

        nearest_distance = info.get("road_factor_distance_to_nearest_ps_km")
        if nearest_distance is None:
            nearest_distance = info.get("distance_to_nearest_ps_km")

        frv_points.append({
            "lat":            float(lat),
            "lon":            float(lon),
            "district":       district,
            "ps":             ps_name,
            "psKey":          ps_key,
            "frvId":          str(info.get("frv_id") or "N/A"),
            "avgResponse":    avg_response,
            "maxResponse":    float(info.get("max_response_time_min") or 0),
            "incidents":      int(info.get("size") or 0),
            "nearestPs":      str(info.get("nearest_police_station") or "N/A"),
            "nearestDistance": float(nearest_distance) if nearest_distance is not None else None,
            "transfer_from_ps": info.get("transfer_from_ps"),
            "transfer_to_ps": info.get("transfer_to_ps"),

            "patrolRouteLength": info.get("patrolRouteLength"),
            "coverage": info.get("coverage"),
            "assignedPatrolZones": info.get("assignedPatrolZones"),
        })

    return frv_points


def _match_ps_for_point(point: Point, district: str, ps_lookup: list[dict]) -> dict | None:
    same_district  = [item for item in ps_lookup if item["district"] == district]
    other_district = [item for item in ps_lookup if item["district"] != district]
    for item in same_district + other_district:
        geometry = item["geometry"]
        if geometry.contains(point) or geometry.intersects(point):
            return item
    return None


def _attach_frv_counts(ps_points: list[dict], frv_points: list[dict]) -> None:
    counts: dict[str, int] = {}
    for point in frv_points:
        if point.get("psKey"):
            counts[point["psKey"]] = counts.get(point["psKey"], 0) + 1
    for point in ps_points:
        point["frvCount"] = counts.get(point["psKey"], 0)


def _build_optimized_deployment_points(
    current_points: list[dict],
    transfer_rows: list[dict],
    resimulation_rows: list[dict],
    ps_point_lookup: dict[str, dict],
) -> list[dict]:
    optimized = [dict(point) for point in current_points]
    if not transfer_rows and not resimulation_rows:
        return optimized

    response_by_key = {}
    for row in resimulation_rows:
        key = _normalize_ps_key(row.get("district"), row.get("ps"))
        response_by_key[key] = {
            "before": _safe_float(row.get("before_avg_response_min")),
            "after": _safe_float(row.get("after_avg_response_min")),
        }

    current_by_key: dict[str, list[dict]] = {}
    for point in optimized:
        current_by_key.setdefault(point.get("psKey") or "", []).append(point)

    moved_ids: set[str] = set()
    moved_points: list[dict] = []

    for transfer in transfer_rows:
        donor_key = _normalize_ps_key(transfer.get("donor_district"), transfer.get("donor_ps"))
        receiver_key = _normalize_ps_key(transfer.get("receiver_district"), transfer.get("receiver_ps"))
        count = int(_safe_float(transfer.get("frvs_moved")) or 0)
        if count <= 0:
            continue

        donor_pool = current_by_key.get(donor_key, [])
        receiver = ps_point_lookup.get(receiver_key)
        donor = ps_point_lookup.get(donor_key)
        for index in range(count):
            source = donor_pool.pop(0) if donor_pool else {
                "frvId": f"{transfer.get('donor_ps') or 'TRANSFER'}-{index + 1}",
                "lat": donor.get("lat") if donor else None,
                "lon": donor.get("lon") if donor else None,
                "avgResponse": 0,
            }
            moved_ids.add(str(source.get("frvId")))
            moved = dict(source)
            moved.update({
                "district": transfer.get("receiver_district") or "",
                "ps": transfer.get("receiver_ps") or "",
                "psKey": receiver_key,
                "lat": receiver.get("lat") if receiver else source.get("lat"),
                "lon": receiver.get("lon") if receiver else source.get("lon"),
                "transfer_from_ps": transfer.get("donor_ps") or "",
                "transfer_to_ps": transfer.get("receiver_ps") or "",
                "transfer_from_district": transfer.get("donor_district") or "",
                "transfer_to_district": transfer.get("receiver_district") or "",
                "is_transferred": True,
            })
            _attach_response_fields(moved, response_by_key)
            moved_points.append(moved)

    optimized = [point for point in optimized if str(point.get("frvId")) not in moved_ids]
    optimized.extend(moved_points)
    for point in optimized:
        _attach_response_fields(point, response_by_key)
    return optimized


def _attach_response_fields(point: dict, response_by_key: dict[str, dict]) -> None:
    response = response_by_key.get(point.get("psKey") or "")
    if not response:
        return
    if response.get("before") is not None:
        point["avg_response_before"] = response["before"]
    if response.get("after") is not None:
        point["avg_response_after"] = response["after"]
        point["avgResponse"] = response["after"]
        point["avg_response_time_min"] = response["after"]


def _attach_patrol_metrics(frv_points: list[dict], patrol_routes: list[dict]) -> None:
    by_frv = {route.get("frvId"): route for route in patrol_routes if route.get("frvId")}
    by_ps: dict[str, list[dict]] = {}
    for route in patrol_routes:
        by_ps.setdefault(route.get("ps") or "", []).append(route)

    for point in frv_points:
        route = by_frv.get(point.get("frvId"))
        if route is None:
            ps_routes = by_ps.get(point.get("ps") or "", [])
            route = ps_routes[0] if ps_routes else None
        if route is None:
            continue
        point["patrolRouteLength"] = route.get("routeLengthKm")
        point["assignedPatrolZones"] = route.get("waypointCount")
        point["coverage"] = route.get("coverage")


def _deployment_ps_name(info: dict) -> str:
    for key in ("nearest_police_station", "police_station"):
        value = str(info.get(key) or "").strip()
        if value and value.upper() != "N/A":
            return value
    return ""


def _normalize_ps_key(district: object, ps: object) -> str:
    return f"{str(district or '').strip()}||{str(ps or '').strip()}"


def _safe_float(value: object) -> float | None:
    try:
        if value == "":
            return None
        result = float(value)
    except Exception:
        return None
    if pd.isna(result):
        return None
    return result


def _safe_int(value: object) -> int | None:
    number = _safe_float(value)
    return int(number) if number is not None else None


def _safe_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _json_safe_row(row: dict) -> dict:
    safe = {}
    for key, value in row.items():
        if pd.isna(value):
            safe[key] = ""
        elif hasattr(value, "item"):
            safe[key] = value.item()
        else:
            safe[key] = value
    return safe


# ---------------------------------------------------------------------------
# Popup / formatting helpers  (kept here so map_data.json can carry pre-built
# popup HTML — avoids duplicating logic in JS)
# ---------------------------------------------------------------------------

def _build_frv_popup(info: dict) -> str:
    avg_resp    = float(info.get("avgResponse") or 0)
    max_resp    = float(info.get("maxResponse") or 0)
    resp_color  = "#159947" if avg_resp <= 10 else "#dc2626"
    nearest_ps  = escape(str(info.get("nearestPs") or "N/A"))
    nd          = info.get("nearestDistance")
    nearest_lbl = nearest_ps if nd is None else f"{nearest_ps} ({float(nd):.2f} km)"
    return (
        f'<div style="font-family:Segoe UI,Arial,sans-serif;min-width:285px;color:#17212b">'
        f'<h3 style="margin:0 0 8px 0;font-size:17px;color:{resp_color}">'
        f'FRV {escape(str(info.get("frvId", "N/A")))}</h3>'
        f'<table style="width:100%;font-size:13px;border-collapse:collapse">'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>District</b></td>'
        f'<td style="text-align:right">{escape(str(info.get("district", "N/A")))}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Police Station</b></td>'
        f'<td style="text-align:right">{escape(str(info.get("ps") or "N/A"))}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Nearest PS</b></td>'
        f'<td style="text-align:right">{nearest_lbl}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Incidents</b></td>'
        f'<td style="text-align:right">{int(info.get("incidents") or 0):,}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Max response</b></td>'
        f'<td style="text-align:right">{_format_minutes(max_resp)}</td></tr>'
        f'</table>'
        f'<div style="margin-top:10px;padding:8px;border-radius:6px;background:{resp_color};'
        f'color:#fff;text-align:center;font-weight:700">Avg response: {_format_minutes(avg_resp)}</div>'
        f'</div>'
    )


def _build_ps_popup(info: dict) -> str:
    return (
        f'<div style="font-family:Segoe UI,Arial,sans-serif;min-width:260px;color:#17212b">'
        f'<h3 style="margin:0 0 8px 0;font-size:17px;color:#1f6feb">Police Station</h3>'
        f'<table style="width:100%;font-size:13px;border-collapse:collapse">'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Name</b></td>'
        f'<td style="text-align:right">{escape(str(info.get("ps") or "N/A"))}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>District</b></td>'
        f'<td style="text-align:right">{escape(str(info.get("district") or "N/A"))}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>FRVs in PS</b></td>'
        f'<td style="text-align:right">{int(info.get("frvCount") or 0)}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Nearest FRV distance</b></td>'
        f'<td style="text-align:right">{float(info.get("nearestDistance") or 0):.2f} km</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Avg FRV distance</b></td>'
        f'<td style="text-align:right">{float(info.get("avgDistance") or 0):.2f} km</td></tr>'
        f'</table></div>'
    )


def _format_minutes(value: object) -> str:
    try:
        total_seconds = int(round(float(value) * 60))
    except Exception:
        total_seconds = 0
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


# ---------------------------------------------------------------------------
# Geometry utilities
# ---------------------------------------------------------------------------

def _geometry_bounds(geometry) -> list:
    min_lon, min_lat, max_lon, max_lat = geometry.bounds
    return [[min_lat, min_lon], [max_lat, max_lon]]


def _combined_bounds(bounds_list) -> list | None:
    bounds = list(bounds_list)
    if not bounds:
        return None
    min_lat = min(item[0][0] for item in bounds)
    min_lon = min(item[0][1] for item in bounds)
    max_lat = max(item[1][0] for item in bounds)
    max_lon = max(item[1][1] for item in bounds)
    return [[min_lat, min_lon], [max_lat, max_lon]]


def _map_center(frv_points: list[dict], settings: Settings) -> tuple[float, float]:
    if not frv_points:
        return settings.map_center_lat, settings.map_center_lon
    return (
        sum(p["lat"] for p in frv_points) / len(frv_points),
        sum(p["lon"] for p in frv_points) / len(frv_points),
    )


def _data_output_path(map_html_path: Path) -> Path:
    """Return the path where map_data.json is written next to the HTML."""
    return map_html_path.parent / "map_data.json"
