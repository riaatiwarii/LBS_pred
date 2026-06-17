from __future__ import annotations

import csv
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

from .config import Settings

logger = logging.getLogger(__name__)


def parse_lbs_response_xml(xml_string: object) -> tuple[str | None, str | None, str | None]:
    try:
        if xml_string is None or not str(xml_string).strip():
            return None, None, None
        root = ET.fromstring(str(xml_string).strip())
        lat_elem = root.find(".//latitude")
        lon_elem = root.find(".//longitude")
        address_elem = root.find(".//address1")
        return (
            lat_elem.text if lat_elem is not None else None,
            lon_elem.text if lon_elem is not None else None,
            address_elem.text if address_elem is not None else None,
        )
    except Exception:
        return None, None, None


def clean_csv_value(value: object) -> object:
    if isinstance(value, str) and value.startswith('="') and value.endswith('"'):
        return value[2:-1]
    return value


def _load_processed_files(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def combine_raw_lbs_files(settings: Settings, incremental: bool = False) -> Path:
    settings.ensure_dirs()
    processed_files = _load_processed_files(settings.checkpoint_file) if incremental else set()
    rows: list[dict[str, object]] = []

    if incremental and settings.combined_csv.exists():
        rows.extend(pd.read_csv(settings.combined_csv).to_dict("records"))

    new_files = 0
    new_rows = 0
    raw_csvs = sorted(settings.raw_data_dir.rglob("*.csv"))
    logger.info("Scanning %s raw CSV files under %s", len(raw_csvs), settings.raw_data_dir)

    for file_path in raw_csvs:
        file_key = str(file_path.resolve())
        if file_key in processed_files:
            continue

        try:
            with file_path.open("r", encoding="utf-8", errors="ignore", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    lat, lon, address = parse_lbs_response_xml(row.get("Response"))
                    rows.append(
                        {
                            "callid": clean_csv_value(row.get("CallId")),
                            "date": row.get("AddDate"),
                            "address": address,
                            "latitude": lat,
                            "longitude": lon,
                        }
                    )
                    new_rows += 1
        except Exception as exc:
            logger.warning("Could not process %s: %s", file_path, exc)
            continue

        processed_files.add(file_key)
        new_files += 1
        if new_files % 500 == 0:
            logger.info("Processed %s new files and %s new rows", new_files, new_rows)

    if not rows:
        raise FileNotFoundError(f"No LBS records found under {settings.raw_data_dir}")

    pd.DataFrame(rows).to_csv(settings.combined_csv, index=False)
    settings.checkpoint_file.write_text("\n".join(sorted(processed_files)), encoding="utf-8")
    logger.info("Saved %s rows to %s", len(rows), settings.combined_csv)
    return settings.combined_csv


def resolve_combined_csv(settings: Settings) -> Path:
    candidates = [
        settings.combined_csv,
        settings.legacy_root / "district_wise_output" / "final_combined_lbs_data.csv",
        settings.legacy_root / "output" / "final_combined_lbs_data.csv",
        settings.legacy_root / "final_combined_lbs_data.csv",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("No combined LBS CSV found. Run ingest first.")
