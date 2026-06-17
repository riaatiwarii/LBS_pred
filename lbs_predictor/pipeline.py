from __future__ import annotations

import logging

from .cleaning import load_and_clean_incidents, write_cleaning_audit
from .clustering import run_district_level_clustering
from .config import Settings
from .geo import assign_boundaries
from .ingestion import combine_raw_lbs_files, resolve_combined_csv
from .clean_mapping import generate_map
from .optimization_refactored import run_full_optimization_refactored
from .patrol_routing import generate_patrol_routes
from .demand import compute_demand_scores

logger = logging.getLogger(__name__)


def run_pipeline(
    settings: Settings,
    skip_ingest: bool = False,
    incremental: bool = False,
    skip_map: bool = False,
    run_optimization: bool = False,
) -> dict:
    settings.ensure_dirs()

    if skip_ingest:
        combined_csv = resolve_combined_csv(settings)
        logger.info("Skipping ingest; using %s", combined_csv)
    else:
        combined_csv = combine_raw_lbs_files(settings, incremental=incremental)

    incidents, audit = load_and_clean_incidents(
        combined_csv,
        settings.analysis_window_days
    )

    if len(incidents) < settings.min_cluster_size:
        raise RuntimeError(
            f"Only {len(incidents)} valid incidents available after cleaning"
        )

    incidents = assign_boundaries(incidents, settings)
    print("\nCOLUMNS AFTER assign_boundaries():")
    print(incidents.columns.tolist())
    print()

    TEST_DISTRICT = None 

    district_col = None

    for col in incidents.columns:
        if col.lower() == "district":
            district_col = col
            break

    if TEST_DISTRICT and district_col:
        incidents = incidents[
            incidents[district_col] == TEST_DISTRICT
        ].copy()

        logger.warning(
            "DEBUG MODE ENABLED: %s (%d incidents)",
            TEST_DISTRICT,
            len(incidents)
        )
    else:
        print("No district column found")
    clustered, medoids, district_summaries = run_district_level_clustering(incidents, settings)
    write_cleaning_audit(audit, settings.cleaning_audit_csv)

    map_path = None
    

    optimization_result = None
    patrol_routes_csv   = None
    if run_optimization:
        optimization_result = run_full_optimization_refactored(settings, incidents, medoids)

        print("\n===== OPTIMIZATION RESULT =====")
        print(type(optimization_result))

        if isinstance(optimization_result, dict):
            print("Keys:")
            print(list(optimization_result.keys()))

            for k, v in optimization_result.items():
                print(f"\n{k}")
                print(type(v))

                try:
                    if hasattr(v, "head"):
                        print(v.head())
                except:
                    pass

        print("==============================\n")

        redistribution_df = optimization_result["redistribution_df"]
        print("\n===== TRANSFER CHECK =====")

        if (
            "current_ps" in redistribution_df.columns
            and
            "assigned_ps" in redistribution_df.columns
        ):

            moved = redistribution_df[
                redistribution_df["current_ps"]
                !=
                redistribution_df["assigned_ps"]
            ]

            print("FRVs moved:", len(moved))

        else:
            print("Transfer columns missing")

        print("==========================\n")
        

        print("\n===== REDISTRIBUTION DEBUG =====")
        print(redistribution_df.head())
        print("Rows:", len(redistribution_df))
        print("Columns:", redistribution_df.columns.tolist())
        print("===============================\n")

        # Generate patrol routes using redistributed FRV locations.
        # final_locations comes from the redistribution_df which has
        # per-PS FRV counts after transfers.  We build a location table
        # from the medoids (lat/lon) joined with PS assignments.
        try:
            redistribution_df = optimization_result["redistribution_df"]
            
            # Build a minimal final_locations DataFrame:
            # one row per medoid, carrying its ps and coordinates.
            print("\n===== MEDOID SAMPLE =====")

            for k, v in list(medoids.items())[:3]:
                print("KEY:", k)
                print("VALUE:", v)

            print("=========================\n")
            import pandas as pd
            medoid_rows = [
                {
                    "frv_id":    str(info.get("frv_id", f"FRV-{mid}")),
                    "ps":        str(info.get("police_station", "Unknown")),
                    "latitude":  float(info["latitude"]),
                    "longitude": float(info["longitude"]),
                }
                for mid, info in medoids.items()
                if info.get("police_station") not in (None, "District-level", "Unknown")
            ]
            if medoid_rows:
                final_locations = pd.DataFrame(medoid_rows)
                patrol_df = generate_patrol_routes(settings, final_locations, incidents)
                patrol_routes_csv = str(settings.output_dir / "patrol_routes.csv")
                logger.info("Patrol routes written → %s", patrol_routes_csv)
            else:
                logger.warning(
                    "No PS-level medoid locations available for patrol routing. "
                    "Run clustering with PS-level medoids to enable patrol routes."
                )
        except Exception as exc:
            logger.warning("Patrol route generation failed (non-fatal): %s", exc)

    # Always run demand analysis to produce district/PS demand CSVs
    demand_result = compute_demand_scores(settings, incidents, medoids)
    if not skip_map:
        map_path = generate_map(settings)
    return {
        "n_total": len(clustered),
        "n_medoids": len(medoids),
        "n_noise": int((clustered["cluster_label"] == -1).sum()),
        "districts": len(district_summaries),
        "clustered_csv": str(settings.clustered_csv),
        "medoids_json": str(settings.medoids_json),
        "map_html": map_path,
        "optimization": optimization_result,
        "patrol_routes_csv": patrol_routes_csv,
        "district_demand_csv": str(demand_result["district_path"]),
        "ps_demand_csv": str(demand_result["ps_path"]),
    }