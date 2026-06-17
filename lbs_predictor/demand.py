from __future__ import annotations

import logging
from typing import Dict

import numpy as np
import pandas as pd

from .config import Settings

logger = logging.getLogger(__name__)


def _normalize_series(s: pd.Series) -> pd.Series:
    if s.empty:
        return s
    mi = s.min()
    ma = s.max()
    if ma == mi:
        return s.apply(lambda _: 0.0)
    return (s - mi) / (ma - mi)


def compute_demand_scores(settings: Settings, incidents: pd.DataFrame, medoids: Dict[int, dict]) -> Dict[str, pd.DataFrame]:
    """Compute district and police-station demand scores and write CSVs.

    Inputs:
    - incidents: DataFrame with columns including 'district' and 'ps'
    - medoids: dict of medoid info (must include 'district', 'police_station' or 'police_station' key may vary)

    Outputs:
    - district_df, ps_df (as DataFrames)
    """
    df = incidents.copy()
    df["district"] = df.get("district")
    # police station column may be 'ps' from assign_boundaries
    ps_col = "ps" if "ps" in df.columns else "police_station"
    df["ps"] = df.get(ps_col)

    # Incident counts per district / ps
    district_counts = df.groupby("district").size().rename("incident_count").astype(int)
    ps_counts = df.groupby("ps").size().rename("incident_count").astype(int)

    # Hotspot severity per district / ps from medoids
    medoid_rows = []
    for mid, info in (medoids or {}).items():
        district = info.get("district") or info.get("dst_nme") or "Unknown"
        ps = info.get("police_station") or info.get("ps") or "District-level"
        size = int(info.get("size", 0))
        avg_rt = float(info.get("avg_response_time_min") or 0.0)
        medoid_rows.append({"district": district, "ps": ps, "size": size, "avg_response_time_min": avg_rt})

    medoid_df = pd.DataFrame(medoid_rows)
    if medoid_df.empty:
        medoid_df = pd.DataFrame(columns=["district", "ps", "size", "avg_response_time_min"])

    district_severity = medoid_df.groupby("district")["size"].sum().rename("hotspot_severity")
    ps_severity = medoid_df.groupby("ps")["size"].sum().rename("hotspot_severity")

    # Response penalty: mean response time per district/ps
    district_rt = medoid_df.groupby("district")["avg_response_time_min"].mean().rename("avg_response_time_min")
    ps_rt = medoid_df.groupby("ps")["avg_response_time_min"].mean().rename("avg_response_time_min")

    # Coverage gap: fraction of medoids with avg_response_time > threshold
    threshold = getattr(settings, "demand_response_threshold_min", 10.0)
    def coverage_gap(group):
        if len(group) == 0:
            return 1.0
        return float((group["avg_response_time_min"] > threshold).sum()) / len(group)

    district_gap = medoid_df.groupby("district").apply(coverage_gap).rename("coverage_gap")
    ps_gap = medoid_df.groupby("ps").apply(coverage_gap).rename("coverage_gap")

    # Assemble district dataframe
    district_df = pd.concat([district_counts, district_severity, district_rt, district_gap], axis=1).fillna(0)
    ps_df = pd.concat([ps_counts, ps_severity, ps_rt, ps_gap], axis=1).fillna(0)

    # Normalize components
    district_df["incident_density_norm"] = _normalize_series(district_df["incident_count"]) 
    district_df["hotspot_severity_norm"] = _normalize_series(district_df["hotspot_severity"]) 
    district_df["response_penalty_norm"] = _normalize_series(district_df["avg_response_time_min"]) 
    district_df["coverage_gap_norm"] = _normalize_series(district_df["coverage_gap"]) 

    ps_df["incident_density_norm"] = _normalize_series(ps_df["incident_count"]) 
    ps_df["hotspot_severity_norm"] = _normalize_series(ps_df["hotspot_severity"]) 
    ps_df["response_penalty_norm"] = _normalize_series(ps_df["avg_response_time_min"]) 
    ps_df["coverage_gap_norm"] = _normalize_series(ps_df["coverage_gap"]) 

    # Weights (can be overridden via settings)
    w1 = getattr(settings, "demand_w_incident", 0.4)
    w2 = getattr(settings, "demand_w_hotspot", 0.3)
    w3 = getattr(settings, "demand_w_response", 0.2)
    w4 = getattr(settings, "demand_w_coverage", 0.1)

    district_df["demand_score"] = (
        w1 * district_df["incident_density_norm"]
        + w2 * district_df["hotspot_severity_norm"]
        + w3 * district_df["response_penalty_norm"]
        + w4 * district_df["coverage_gap_norm"]
    )

    ps_df["demand_score"] = (
        w1 * ps_df["incident_density_norm"]
        + w2 * ps_df["hotspot_severity_norm"]
        + w3 * ps_df["response_penalty_norm"]
        + w4 * ps_df["coverage_gap_norm"]
    )

    # Sort and return
    district_df = district_df.sort_values("demand_score", ascending=False)
    ps_df = ps_df.sort_values("demand_score", ascending=False)

    # Save CSVs to outputs
    out_dir = settings.output_dir
    district_path = out_dir / "district_demand.csv"
    ps_path = out_dir / "ps_demand.csv"
    district_df.reset_index().to_csv(district_path, index=False)
    ps_df.reset_index().to_csv(ps_path, index=False)

    logger.info("Wrote district demand to %s and ps demand to %s", district_path, ps_path)

    return {"district_df": district_df, "ps_df": ps_df, "district_path": district_path, "ps_path": ps_path}
