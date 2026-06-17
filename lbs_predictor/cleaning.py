from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


def load_and_clean_incidents(csv_path: Path, analysis_days: int | None = None) -> tuple[pd.DataFrame, dict[str, int]]:
    df = pd.read_csv(csv_path)
    audit = {"loaded_rows": len(df)}

    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")

    before = len(df)
    df = df.dropna(subset=["latitude", "longitude"]).copy()
    audit["dropped_missing_coordinates"] = before - len(df)

    before = len(df)
    df = df[(df["latitude"] != 0.0) & (df["longitude"] != 0.0)].copy()
    audit["dropped_zero_coordinates"] = before - len(df)

    if analysis_days is not None and "date" in df.columns:
        parsed = pd.to_datetime(df["date"], errors="coerce", dayfirst=True)
        if parsed.notna().any():
            cutoff = datetime.now() - timedelta(days=analysis_days)
            before = len(df)
            df = df[parsed >= cutoff].copy()
            audit["dropped_outside_time_window"] = before - len(df)

    before = len(df)
    df = df[
        (df["latitude"] >= 15)
        & (df["latitude"] <= 35)
        & (df["longitude"] >= 68)
        & (df["longitude"] <= 98)
    ].copy()
    audit["dropped_outside_india"] = before - len(df)
    audit["final_rows"] = len(df)

    logger.info("Clean dataset: %s rows from %s", len(df), csv_path)
    return df, audit


def write_cleaning_audit(audit: dict[str, int], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([audit]).to_csv(output_csv, index=False)
