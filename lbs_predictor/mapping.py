from __future__ import annotations

import json
import logging
from html import escape

import folium
import geopandas as gpd
import pandas as pd
from folium.plugins import HeatMap, MarkerCluster
from shapely.geometry import MultiPoint

from .config import Settings

logger = logging.getLogger(__name__)

MAX_HEATMAP_CELLS = 60000
MAX_HOTSPOT_MARKERS = 25000
MAX_NOISE_MARKERS = 10000
MAX_ZONE_POINTS_PER_CLUSTER = 800


def generate_map(settings: Settings) -> str:
    df = pd.read_csv(settings.clustered_csv).dropna(subset=["latitude", "longitude"]).copy()
    medoids = _load_json(settings.medoids_json)
    summaries = _load_json(settings.district_summaries_json)

    mp_df = df[df["district"] != "Outside MP"].copy() if "district" in df.columns else df
    center_lat = float(mp_df["latitude"].mean()) if len(mp_df) else settings.map_center_lat
    center_lon = float(mp_df["longitude"].mean()) if len(mp_df) else settings.map_center_lon

    fmap = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=settings.map_zoom,
        tiles="cartodbdark_matter",
        control_scale=True,
        prefer_canvas=True,
    )

    _add_base_layers(fmap)
    _add_map_css(fmap)
    district_bounds, ps_bounds, district_ps_map = _add_boundaries(fmap, settings, summaries)
    _add_incident_layers(fmap, df, settings)
    _add_hotspot_zones(fmap, df, settings)
    _add_deployments(fmap, medoids)
    _add_area_explorer(fmap, district_bounds, ps_bounds, district_ps_map, settings)

    all_bounds = _combined_bounds(district_bounds.values())
    if all_bounds:
        fmap.fit_bounds(all_bounds)

    folium.LayerControl(collapsed=False).add_to(fmap)
    settings.map_html.parent.mkdir(parents=True, exist_ok=True)
    fmap.save(settings.map_html)
    logger.info("Map saved to %s", settings.map_html)
    return str(settings.map_html)


def _load_json(path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _add_base_layers(fmap: folium.Map) -> None:
    folium.TileLayer("OpenStreetMap", name="Street Map").add_to(fmap)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri",
        name="Satellite",
        overlay=False,
        control=True,
    ).add_to(fmap)
    folium.WmsTileLayer(
        url="https://bhuvan-vec3.nrsc.gov.in/bhuvan/ows",
        layers="india_map",
        name="ISRO Bhuvan Base Map",
        fmt="image/png",
        transparent=True,
        attr="ISRO Bhuvan | NRSC",
        show=True,
    ).add_to(fmap)
    folium.WmsTileLayer(
        url="https://bhuvan-vec3.nrsc.gov.in/bhuvan/ows",
        layers="multispectral_1m",
        name="ISRO Bhuvan Satellite",
        fmt="image/png",
        transparent=True,
        attr="ISRO Bhuvan | NRSC",
        show=True,
    ).add_to(fmap)


def _add_map_css(fmap: folium.Map) -> None:
    css = """
    <style>
      .leaflet-popup-pane { z-index: 3000 !important; }
      .leaflet-tooltip-pane { z-index: 2500 !important; }
      .leaflet-control-layers {
        border-radius: 18px !important;
        padding: 10px !important;
        box-shadow: 0 8px 24px rgba(0,0,0,.28) !important;
      }
      #filter-panel { z-index: 1200 !important; }
    </style>
    """
    fmap.get_root().header.add_child(folium.Element(css))


def _add_boundaries(fmap: folium.Map, settings: Settings, summaries: dict) -> tuple[dict, dict, dict]:
    district_bounds: dict[str, list] = {}
    ps_bounds: dict[str, list] = {}
    district_ps_map: dict[str, list] = {}

    if settings.districts_geojson.exists():
        districts = gpd.read_file(settings.districts_geojson)
        name_col = "dst_nme" if "dst_nme" in districts.columns else "dtname"
        district_bounds = {str(row[name_col]): _geometry_bounds(row.geometry) for _, row in districts.iterrows()}
        districts["incidents"] = districts[name_col].apply(lambda name: summaries.get(name, {}).get("total_incidents", 0))
        districts["frvs"] = districts[name_col].apply(lambda name: summaries.get(name, {}).get("n_frvs", 0))
        districts["avg_resp"] = districts[name_col].apply(lambda name: summaries.get(name, {}).get("avg_response_time_min", 0))

        folium.GeoJson(
            districts,
            name="District Boundaries",
            style_function=lambda feature: {
                "fillColor": _district_fill(feature["properties"].get("incidents", 0)),
                "color": "#0078ff",
                "weight": 1.4,
                "fillOpacity": 0.05,
            },
            tooltip=folium.GeoJsonTooltip(
                fields=[name_col, "incidents", "frvs", "avg_resp"],
                aliases=["District:", "Incidents:", "FRVs:", "Avg Response:"],
                localize=True,
            ),
            popup=folium.GeoJsonPopup(
                fields=[name_col, "incidents", "frvs", "avg_resp"],
                aliases=["District:", "Incidents:", "FRVs:", "Avg Response:"],
                localize=True,
                max_width=280,
            ),
            show=True,
        ).add_to(fmap)

    if settings.police_station_geojson.exists():
        police_stations = gpd.read_file(settings.police_station_geojson)
        for _, row in police_stations.iterrows():
            district = str(row.get("dst_nme", "")).strip()
            ps_name = str(row.get("ps", "")).strip()
            if not district or not ps_name:
                continue
            key = f"{district}||{ps_name}"
            ps_bounds[key] = _geometry_bounds(row.geometry)
            district_ps_map.setdefault(district, []).append({"label": ps_name, "value": key})
        for items in district_ps_map.values():
            items.sort(key=lambda item: item["label"])

        folium.GeoJson(
            police_stations,
            name="Police Station Boundaries",
            style_function=lambda _: {
                "fillColor": "#ff6348",
                "color": "#ff6348",
                "weight": 1.6,
                "fillOpacity": 0.08,
            },
            tooltip=folium.GeoJsonTooltip(
                fields=["ps", "dst_nme"],
                aliases=["Police Station:", "District:"],
                localize=True,
            ),
            popup=folium.GeoJsonPopup(
                fields=["ps", "dst_nme"],
                aliases=["Police Station:", "District:"],
                localize=True,
                max_width=260,
            ),
            show=True,
        ).add_to(fmap)

    return district_bounds, ps_bounds, district_ps_map


def _district_fill(incidents: int | float) -> str:
    if incidents > 50000:
        return "#ff4757"
    if incidents > 10000:
        return "#ffa502"
    if incidents > 1000:
        return "#7bed9f"
    return "#2f3542"


def _geometry_bounds(geometry) -> list:
    min_lon, min_lat, max_lon, max_lat = geometry.bounds
    return [[min_lat, min_lon], [max_lat, max_lon]]


def _add_incident_layers(fmap: folium.Map, df: pd.DataFrame, settings: Settings) -> None:
    mp_df = df[df["district"] != "Outside MP"].copy() if "district" in df.columns else df.copy()

    heat_group = folium.FeatureGroup(name="Incident Heatmap", show=False)
    if len(mp_df):
        heat_df = mp_df.copy()
        heat_df["lat_r"] = heat_df["latitude"].round(3)
        heat_df["lon_r"] = heat_df["longitude"].round(3)
        heat = heat_df.groupby(["lat_r", "lon_r"]).size().reset_index(name="weight")
        heat = heat.nlargest(MAX_HEATMAP_CELLS, "weight")
        HeatMap(
            heat[["lat_r", "lon_r", "weight"]].values.tolist(),
            radius=18,
            blur=16,
            min_opacity=0.25,
            max_zoom=12,
        ).add_to(heat_group)
    heat_group.add_to(fmap)

    hotspot_group = folium.FeatureGroup(name="Hotspot Incidents", show=False)
    hotspot_df = mp_df[mp_df["cluster_label"] != -1].copy()
    if len(hotspot_df):
        hotspot_df["lat_r"] = hotspot_df["latitude"].round(3)
        hotspot_df["lon_r"] = hotspot_df["longitude"].round(3)
        grouped = hotspot_df.groupby(["lat_r", "lon_r", "cluster_label"]).size().reset_index(name="weight")
        grouped = grouped.nlargest(MAX_HOTSPOT_MARKERS, "weight")
        for row in grouped.itertuples(index=False):
            folium.CircleMarker(
                location=[row.lat_r, row.lon_r],
                radius=2 if row.weight < 10 else 4,
                color=settings.hotspot_color,
                fill=True,
                fill_color=settings.hotspot_color,
                fill_opacity=0.58,
                weight=0,
                tooltip=f"Hotspot {int(row.cluster_label)}: {row.weight} incidents",
            ).add_to(hotspot_group)
    hotspot_group.add_to(fmap)

    noise_group = folium.FeatureGroup(name="Noise Points (Grey Outliers)", show=False)
    noise_df = mp_df[mp_df["cluster_label"] == -1].copy()
    if len(noise_df):
        noise_df["lat_r"] = noise_df["latitude"].round(3)
        noise_df["lon_r"] = noise_df["longitude"].round(3)
        grouped = noise_df.groupby(["lat_r", "lon_r"]).size().reset_index(name="weight")
        grouped = grouped.nlargest(MAX_NOISE_MARKERS, "weight")
        for row in grouped.itertuples(index=False):
            folium.CircleMarker(
                location=[row.lat_r, row.lon_r],
                radius=1.5 if row.weight < 10 else 3,
                color="#7f8c8d",
                fill=True,
                fill_color="#7f8c8d",
                fill_opacity=0.38,
                weight=0,
                tooltip=f"Noise: {row.weight} incidents",
            ).add_to(noise_group)
    noise_group.add_to(fmap)


def _add_hotspot_zones(fmap: folium.Map, df: pd.DataFrame, settings: Settings) -> None:
    zone_group = folium.FeatureGroup(name="Hotspot Zones (Coverage Area)", show=True)
    hotspot_df = df[df["cluster_label"] != -1].copy()
    if "district" in hotspot_df.columns:
        hotspot_df = hotspot_df[hotspot_df["district"] != "Outside MP"]
    if hotspot_df.empty:
        zone_group.add_to(fmap)
        return

    hotspot_df["lat_r"] = hotspot_df["latitude"].round(3)
    hotspot_df["lon_r"] = hotspot_df["longitude"].round(3)
    hotspot_df = hotspot_df.drop_duplicates(subset=["lat_r", "lon_r", "cluster_label"])

    for cluster_id in sorted(hotspot_df["cluster_label"].dropna().unique()):
        cluster_df = hotspot_df[hotspot_df["cluster_label"] == cluster_id]
        if len(cluster_df) < 3:
            continue
        if len(cluster_df) > MAX_ZONE_POINTS_PER_CLUSTER:
            cluster_df = cluster_df.sample(MAX_ZONE_POINTS_PER_CLUSTER, random_state=42)
        points = [(float(row.lon_r), float(row.lat_r)) for row in cluster_df.itertuples()]
        hull = MultiPoint(points).convex_hull
        if hull.geom_type != "Polygon":
            continue
        locations = [[lat, lon] for lon, lat in hull.exterior.coords]
        folium.Polygon(
            locations=locations,
            color=settings.hotspot_color,
            weight=2,
            fill=True,
            fill_color=settings.hotspot_color,
            fill_opacity=0.12,
            tooltip=f"Hotspot Zone #{int(cluster_id)}",
        ).add_to(zone_group)
    zone_group.add_to(fmap)


def _add_deployments(fmap: folium.Map, medoids: dict) -> None:
    deployment_group = folium.FeatureGroup(name="FRV Deployment & Routes", show=True)
    marker_cluster = MarkerCluster().add_to(deployment_group)
    routes_group = folium.FeatureGroup(name="Calculated Routes (Spider)", show=False)
    nearest_ps_group = folium.FeatureGroup(name="Nearest Police Stations", show=True)
    nearest_ps_markers: dict[tuple, dict] = {}

    for info in medoids.values():
        lat = info.get("latitude")
        lon = info.get("longitude")
        if lat is None or lon is None:
            continue

        avg_response = float(info.get("avg_response_time_min") or 0)
        marker_color = "green" if avg_response <= 10 else "red"
        folium.Marker(
            location=[lat, lon],
            popup=folium.Popup(_medoid_popup(info), max_width=340),
            tooltip=f"FRV {escape(str(info.get('frv_id', 'N/A')))}",
            icon=folium.Icon(color=marker_color, icon="star", prefix="fa"),
        ).add_to(marker_cluster)

        for point in info.get("sample_points", []) or []:
            folium.PolyLine(
                locations=[[lat, lon], point],
                color="#ffd32a" if marker_color == "green" else "#ff4757",
                weight=1.4,
                opacity=0.42,
                dash_array="5, 5",
                tooltip="Sampled incident route",
            ).add_to(routes_group)

        ps_lat = info.get("nearest_ps_latitude")
        ps_lon = info.get("nearest_ps_longitude")
        ps_name = info.get("nearest_police_station")
        if ps_lat is not None and ps_lon is not None:
            key = (round(float(ps_lat), 6), round(float(ps_lon), 6), ps_name)
            entry = nearest_ps_markers.setdefault(
                key,
                {
                    "latitude": float(ps_lat),
                    "longitude": float(ps_lon),
                    "name": ps_name,
                    "district": info.get("district"),
                    "base_location": info.get("nearest_base_location"),
                    "frv_count": 0,
                    "distances": [],
                },
            )
            entry["frv_count"] += 1
            distance = info.get("road_factor_distance_to_nearest_ps_km")
            if distance is None:
                distance = info.get("distance_to_nearest_ps_km")
            if distance is not None:
                entry["distances"].append(float(distance))

    for ps_info in nearest_ps_markers.values():
        folium.Marker(
            location=[ps_info["latitude"], ps_info["longitude"]],
            popup=folium.Popup(_nearest_ps_popup(ps_info), max_width=330),
            tooltip=f"Nearest PS: {escape(str(ps_info.get('name') or 'N/A'))}",
            icon=folium.Icon(color="green", icon="star", prefix="fa"),
        ).add_to(nearest_ps_group)

    routes_group.add_to(fmap)
    nearest_ps_group.add_to(fmap)
    deployment_group.add_to(fmap)


def _medoid_popup(info: dict) -> str:
    sample_rows = ""
    for index, point in enumerate(info.get("sample_points_details", []) or [], 1):
        sample_rows += (
            "<tr>"
            f"<td>Pt {index}</td>"
            f"<td style='text-align:right'>{point.get('distance_km', 0):.2f} km</td>"
            f"<td style='text-align:right'><b>{_format_minutes(point.get('time_min', 0))}</b></td>"
            "</tr>"
        )
    sample_table = ""
    if sample_rows:
        sample_table = (
            "<hr style='border:0;border-top:1px dashed #555;margin:8px 0'>"
            "<div style='font-size:12px;font-weight:700;color:#444;margin:7px 0'>Sampled Incident Points (5 samples):</div>"
            "<table style='width:100%;font-size:11px;border-collapse:collapse'>"
            f"{sample_rows}</table>"
        )

    avg_resp = float(info.get("avg_response_time_min") or 0)
    max_resp = float(info.get("max_response_time_min") or 0)
    resp_color = "#2ed573" if avg_resp <= 5 else "#ffa502" if avg_resp <= 10 else "#ff4757"
    nearest_ps = escape(str(info.get("nearest_police_station") or "N/A"))
    nearest_distance = info.get("road_factor_distance_to_nearest_ps_km")
    if nearest_distance is None and info.get("distance_to_nearest_ps_km") is not None:
        nearest_distance = info.get("distance_to_nearest_ps_km")
    nearest_label = nearest_ps if nearest_distance is None else f"{nearest_ps} ({float(nearest_distance):.2f} km)"
    return f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;min-width:315px;color:#333;padding:3px">
      <h3 style="margin:0 0 8px 0;color:#ff4757;font-size:18px">Deploy {escape(str(info.get('frv_id', 'N/A')))}</h3>
      <table style="width:100%;font-size:13px;border-collapse:collapse">
        <tr style="border-bottom:1px solid #eee"><td style="padding:5px 0;color:#777"><b>District:</b></td><td style="text-align:right"><b>{escape(str(info.get('district', 'N/A')))}</b></td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:5px 0;color:#777"><b>Placement Type:</b></td><td style="text-align:right"><b>{escape(str(info.get('police_station', 'N/A')))}</b></td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:5px 0;color:#777"><b>Nearest PS:</b></td><td style="text-align:right">{nearest_label}</td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:5px 0;color:#777"><b>Total Incidents:</b></td><td style="text-align:right"><b>{int(info.get('size') or 0):,}</b></td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:5px 0;color:#777"><b>Avg Coverage Radius:</b></td><td style="text-align:right">{float(info.get('avg_radius_km') or 0):.2f} km</td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:5px 0;color:#777"><b>Max Coverage Radius:</b></td><td style="text-align:right">{float(info.get('max_radius_km') or 0):.2f} km</td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:5px 0;color:#777"><b>Max Response Time:</b></td><td style="text-align:right;color:#ff4757"><b>{_format_minutes(max_resp)}</b></td></tr>
      </table>
      {sample_table}
      <div style="background:{resp_color};color:#fff;padding:10px;border-radius:7px;text-align:center;margin-top:12px;font-weight:700;font-size:14px;box-shadow:0 3px 8px rgba(0,0,0,.15)">
        Avg Response Time: {_format_minutes(avg_resp)}
      </div>
    </div>
    """


def _nearest_ps_popup(info: dict) -> str:
    distances = info.get("distances") or []
    nearest = min(distances) if distances else 0.0
    average = sum(distances) / len(distances) if distances else 0.0
    return f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;min-width:305px;color:#333;padding:3px">
      <h3 style="margin:0 0 10px 0;color:#2ed573;font-size:18px">Nearest Police Station</h3>
      <table style="width:100%;font-size:13px;border-collapse:collapse">
        <tr style="border-bottom:1px solid #eee"><td style="padding:6px 0;color:#777"><b>Name:</b></td><td style="text-align:right"><b>{escape(str(info.get('name') or 'N/A'))}</b></td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:6px 0;color:#777"><b>District:</b></td><td style="text-align:right">{escape(str(info.get('district') or 'N/A'))}</td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:6px 0;color:#777"><b>Base Location:</b></td><td style="text-align:right">{escape(str(info.get('base_location') or 'N/A'))}</td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:6px 0;color:#777"><b>Nearest FRVs:</b></td><td style="text-align:right">{int(info.get('frv_count') or 0)}</td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:6px 0;color:#777"><b>Nearest FRV Distance:</b></td><td style="text-align:right">{nearest:.2f} km</td></tr>
        <tr style="border-bottom:1px solid #eee"><td style="padding:6px 0;color:#777"><b>Avg FRV Distance:</b></td><td style="text-align:right">{average:.2f} km</td></tr>
      </table>
    </div>
    """


def _format_minutes(value: object) -> str:
    try:
        total_seconds = int(round(float(value) * 60))
    except Exception:
        total_seconds = 0
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _add_area_explorer(fmap: folium.Map, district_bounds: dict, ps_bounds: dict, district_ps_map: dict, settings: Settings) -> None:
    panel = f"""
    <style>
      #filter-panel label {{ display:block; }}
      .explorer-select {{
        height: 40px;
        padding: 0 12px;
        margin: 6px 0 12px 0;
        background: #222;
        color: #fff;
        border: 1px solid #626262;
        border-radius: 6px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        font-size: 14px;
        cursor: pointer;
        user-select: none;
      }}
      .explorer-select.disabled {{
        color: #aaa;
        cursor: default;
      }}
      .explorer-menu {{
        display: none;
        max-height: 285px;
        overflow-y: auto;
        margin: -12px 0 12px 0;
        background: rgba(31,31,31,.95);
        border: 1px solid #555;
        border-top: 0;
        border-radius: 6px;
        color: #fff;
        box-shadow: 0 12px 24px rgba(0,0,0,.38);
        position: relative;
        z-index: 1300;
      }}
      .explorer-menu.open {{ display: block; }}
      .explorer-option {{
        padding: 7px 14px;
        font-size: 14px;
        cursor: pointer;
        white-space: nowrap;
      }}
      .explorer-option:hover,
      .explorer-option.active {{
        background: #1e6bd6;
      }}
      .explorer-menu::-webkit-scrollbar {{ width: 10px; }}
      .explorer-menu::-webkit-scrollbar-track {{ background: #111; }}
      .explorer-menu::-webkit-scrollbar-thumb {{ background: #555; border-radius: 8px; }}
      @media (max-width: 760px) {{
        #filter-panel {{ left: 14px !important; width: calc(100vw - 28px) !important; }}
      }}
    </style>
    <div id="filter-panel" style="
      position:fixed;top:86px;left:82px;z-index:1200;
      width:min(420px,calc(100vw - 110px));
      background:rgba(0,0,0,.86);color:#fff;
      padding:16px 18px 16px 18px;border-radius:18px;
      font-family:'Segoe UI',Arial,sans-serif;
      box-shadow:0 12px 40px rgba(0,0,0,.55);
    ">
      <h3 style="margin:0 0 12px 0;color:#ff6348;font-size:18px;font-weight:700">
        <span style="color:#5dade2;margin-right:7px">&#128269;</span>Area Explorer
      </h3>
      <label style="font-size:12px;color:#aaa">Select District:</label>
      <div id="district-select" class="explorer-select" onclick="toggleExplorerMenu(event, 'district-menu')">
        <span id="district-label">-- View All MP --</span><span>&#9662;</span>
      </div>
      <div id="district-menu" class="explorer-menu"></div>
      <label style="font-size:12px;color:#aaa">Select Police Station:</label>
      <div id="ps-select" class="explorer-select disabled" onclick="togglePsMenu(event)">
        <span id="ps-label">-- Select District First --</span><span>&#9662;</span>
      </div>
      <div id="ps-menu" class="explorer-menu"></div>
      <div id="filter-info" style="font-size:12px;color:#aaa;margin-top:5px"></div>
    </div>

    <script>
    var distPsMap = {json.dumps(district_ps_map)};
    var distBounds = {json.dumps(district_bounds)};
    var psBounds = {json.dumps(ps_bounds)};
    function getMap() {{
        for (const key in window) {{
            const obj = window[key];

            if (
                obj &&
                typeof obj.fitBounds === "function" &&
                typeof obj.setView === "function"
            ) {{
                return obj;
            }}
        }}

        console.error("Leaflet map not found");
        return null;
    }}
    var selectedDistrict = '';
    var selectedPs = '';
    var allBounds = {_combined_bounds(district_bounds.values())};
    var explorerInitialized = false;

    function initExplorer() {{
      if (explorerInitialized) return;
      explorerInitialized = true;
      buildDistrictMenu();
      buildEmptyPsMenu();
      var panel = document.getElementById('filter-panel');
      if (window.L && L.DomEvent) {{
        L.DomEvent.disableClickPropagation(panel);
        L.DomEvent.disableScrollPropagation(panel);
      }}
    }}

    initExplorer();
    window.addEventListener('DOMContentLoaded', initExplorer);
    window.addEventListener('load', initExplorer);

    document.addEventListener('click', function(event) {{
      var panel = document.getElementById('filter-panel');
      if (panel && !panel.contains(event.target)) {{
        closeExplorerMenus();
      }}
    }});

    function closeExplorerMenus() {{
      document.getElementById('district-menu').classList.remove('open');
      document.getElementById('ps-menu').classList.remove('open');
    }}

    function toggleExplorerMenu(event, menuId) {{
      event.stopPropagation();
      initExplorer();
      var menu = document.getElementById(menuId);
      var wasOpen = menu.classList.contains('open');
      closeExplorerMenus();
      if (!wasOpen) menu.classList.add('open');
    }}

    function togglePsMenu(event) {{
      event.stopPropagation();
      if (!selectedDistrict) return;
      toggleExplorerMenu(event, 'ps-menu');
    }}

    function buildDistrictMenu() {{
      var menu = document.getElementById('district-menu');
      menu.innerHTML = '';
      addExplorerOption(menu, '-- View All MP --', '', selectDistrict);
      Object.keys(distPsMap).sort().forEach(function(name) {{
        addExplorerOption(menu, name, name, selectDistrict);
      }});
    }}

    function buildEmptyPsMenu() {{
      var menu = document.getElementById('ps-menu');
      menu.innerHTML = '';
      var option = document.createElement('div');
      option.className = 'explorer-option';
      option.style.color = '#aaa';
      option.textContent = '-- Select District First --';
      menu.appendChild(option);
    }}

    function buildPsMenu(district) {{
      var menu = document.getElementById('ps-menu');
      menu.innerHTML = '';
      addExplorerOption(menu, '-- All PS in ' + district + ' --', '', selectPs);
      (distPsMap[district] || []).forEach(function(item) {{
        addExplorerOption(menu, item.label, item.value, selectPs);
      }});
    }}

    function addExplorerOption(menu, label, value, callback) {{
      var option = document.createElement('div');
      option.className = 'explorer-option';
      option.textContent = label;
      option.onclick = function(event) {{
        event.stopPropagation();
        callback(value, label);
      }};
      option.dataset.value = value;
      menu.appendChild(option);
    }}

    function markActive(menuId, value) {{
      Array.from(document.getElementById(menuId).children).forEach(function(child) {{
        child.classList.toggle('active', child.dataset.value === value);
      }});
    }}

    function selectDistrict(district, label) {{
      selectedDistrict = district;
      selectedPs = '';
      var info = document.getElementById('filter-info');
      document.getElementById('district-label').textContent = label || '-- View All MP --';
      document.getElementById('ps-label').textContent = district ? '-- All PS in ' + district + ' --' : '-- Select District First --';
      document.getElementById('ps-select').classList.toggle('disabled', !district);
      markActive('district-menu', district);
      closeExplorerMenus();

      if (!district) {{
        buildEmptyPsMenu();
        info.innerHTML = '';
        if (allBounds) {{
          getMap().fitBounds(allBounds);
        }} else {{
          getMap().setView([{settings.map_center_lat}, {settings.map_center_lon}], {settings.map_zoom});
        }}
        return;
      }}

      buildPsMenu(district);
      markActive('ps-menu', '');

      if (distBounds[district]) {{
        getMap().fitBounds(distBounds[district]);
      }}
      info.innerHTML = 'Zoomed to ' + district;
    }}

    function selectPs(psKey, psLabel) {{
      selectedPs = psKey;
      var info = document.getElementById('filter-info');
      document.getElementById('ps-label').textContent = psLabel;
      markActive('ps-menu', psKey);
      closeExplorerMenus();
      if (psKey && psBounds[psKey]) {{
        getMap().fitBounds(psBounds[psKey]);
        info.innerHTML = 'Zoomed to PS: ' + psLabel;
      }} else if (selectedDistrict && distBounds[selectedDistrict]) {{
        getMap().fitBounds(distBounds[selectedDistrict]);
        info.innerHTML = 'Zoomed to ' + selectedDistrict;
      }}
    }}
    </script>
    """
    fmap.get_root().html.add_child(folium.Element(panel))


def _combined_bounds(bounds_list) -> list | None:
    bounds = list(bounds_list)
    if not bounds:
        return None
    min_lat = min(item[0][0] for item in bounds)
    min_lon = min(item[0][1] for item in bounds)
    max_lat = max(item[1][0] for item in bounds)
    max_lon = max(item[1][1] for item in bounds)
    return [[min_lat, min_lon], [max_lat, max_lon]]
