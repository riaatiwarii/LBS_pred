from __future__ import annotations

import logging

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from .config import Settings

logger = logging.getLogger(__name__)


def assign_boundaries(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    districts_gdf = gpd.read_file(settings.districts_geojson)
    name_col = "dst_nme" if "dst_nme" in districts_gdf.columns else "dtname"

    points_gdf = gpd.GeoDataFrame(
        df.copy(),
        geometry=[Point(lon, lat) for lon, lat in zip(df["longitude"], df["latitude"])],
        crs="EPSG:4326",
    )

    if districts_gdf.crs is None:
        districts_gdf = districts_gdf.set_crs("EPSG:4326")
    elif districts_gdf.crs != points_gdf.crs:
        districts_gdf = districts_gdf.to_crs("EPSG:4326")

    joined = gpd.sjoin(points_gdf, districts_gdf[[name_col, "geometry"]], how="left", predicate="within")
    joined = joined.rename(columns={name_col: "district"})
    joined["district"] = joined["district"].fillna("Outside MP")
    result = pd.DataFrame(joined.drop(columns=["geometry", "index_right"], errors="ignore"))

    if settings.police_station_geojson.exists():
        ps_gdf = gpd.read_file(settings.police_station_geojson)
        if ps_gdf.crs is None:
            ps_gdf = ps_gdf.set_crs("EPSG:4326")
        elif ps_gdf.crs != points_gdf.crs:
            ps_gdf = ps_gdf.to_crs("EPSG:4326")

        points_gdf = gpd.GeoDataFrame(
            result.copy(),
            geometry=[Point(lon, lat) for lon, lat in zip(result["longitude"], result["latitude"])],
            crs="EPSG:4326",
        )
        ps_joined = gpd.sjoin(points_gdf, ps_gdf[["ps", "dst_nme", "geometry"]], how="left", predicate="within")
        ps_joined["ps"] = ps_joined["ps"].fillna("Outside PS")
        result = pd.DataFrame(ps_joined.drop(columns=["geometry", "index_right", "dst_nme"], errors="ignore"))
        result = result.drop_duplicates(subset=["callid", "latitude", "longitude"], keep="first")
    else:
        result["ps"] = "Outside PS"

    logger.info("Assigned districts to %s incidents", len(result))
    return result
