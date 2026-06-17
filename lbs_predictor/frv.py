from __future__ import annotations

import pandas as pd

from .config import Settings


def normalize_name(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).lower().strip()


def load_frv_allocations(
    settings: Settings,
) -> tuple[pd.DataFrame, dict[str, int], dict[str, list[str]]]:
    """
    Load FRV allocations at Police Station level.

    Returns
    -------
    frv_df : pd.DataFrame
        One row per FRV with columns including:
            frv_id   — unique FRV identifier (UnitID)
            ps       — police station name    (UnitPoliceStation)
            district — full district name     (mapped from UnitDistrict code)
            base_lat / base_lon — base location coordinates

    district_counts : dict[str, int]
        Total FRV count per district (kept for backward compatibility).

    frv_by_district : dict[str, list[str]]
        FRV IDs grouped by district (kept for backward compatibility).

    Note: frv_df now also supports PS-level grouping via the 'ps' column,
    which is required by phase_0_data_validation() in the optimization pipeline.
    """
    frv_df = pd.read_csv(settings.frv_master_csv)

    # Drop test units
    frv_df = frv_df[
        ~frv_df["UnitID"].astype(str).str.contains("TEST", case=False, na=False)
    ].copy()

    # Map district code → full district name
    mapping = pd.read_csv(settings.district_mapping_csv)
    code_to_name = dict(zip(mapping["UnitID"], mapping["UnitCallSign"]))
    frv_df["district"] = frv_df["UnitDistrict"].map(code_to_name)

    # Rename coordinate columns
    frv_df = frv_df.rename(columns={
        "UnitBaseLocation_X": "base_lat",
        "UnitBaseLocation_Y": "base_lon",
    })

    # Expose PS and FRV ID as clean named columns
    # UnitPoliceStation is already present in the CSV — no spatial join needed
    frv_df["ps"]     = frv_df["UnitPoliceStation"].astype(str).str.strip()
    frv_df["frv_id"] = frv_df["UnitID"].astype(str)

    # District-level aggregations (backward compatibility)
    district_counts: dict[str, int] = {}
    frv_by_district: dict[str, list[str]] = {}
    for district, group in frv_df.dropna(subset=["district"]).groupby("district"):
        district_counts[district] = len(group)
        frv_by_district[district] = group["frv_id"].tolist()

    return frv_df, district_counts, frv_by_district