from __future__ import annotations

import argparse
import logging

from .config import get_settings
from .ingestion import combine_raw_lbs_files
from .clean_mapping import generate_map
from .patrol_routing import generate_patrol_routes
from .pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Production LBS Predictor")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run full district-wise FRV optimization pipeline")
    run.add_argument("--skip-ingest", action="store_true", help="Use existing combined CSV")
    run.add_argument("--incremental", action="store_true", help="Only ingest raw files not seen before")
    run.add_argument("--skip-map", action="store_true", help="Do not generate Folium map")
    run.add_argument("--days", type=int, default=None, help="Only analyze incidents from last N days")
    run.add_argument("--min-cluster", type=int, default=None, help="Override HDBSCAN min_cluster_size")
    run.add_argument("--min-samples", type=int, default=None, help="Override HDBSCAN min_samples")
    run.add_argument("--optimize", action="store_true", help="Run optimization steps (sufficiency, transfers, placement, routes)")

    ingest = subparsers.add_parser("ingest", help="Combine raw LBS CSVs into one processed CSV")
    ingest.add_argument("--incremental", action="store_true", help="Only ingest raw files not seen before")

    subparsers.add_parser("map", help="Regenerate map from existing output CSV/JSON")

    patrol = subparsers.add_parser("patrol", help="Regenerate patrol routes from existing medoids/incidents")
    patrol.add_argument("--max-km", type=float, default=15.0, help="Hard route length limit in km (default: 15)")
    patrol.add_argument("--max-min", type=float, default=60.0, help="Hard route duration limit in minutes (default: 60)")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(name)s: %(message)s")

    settings = get_settings()
    if getattr(args, "days", None) is not None:
        settings.analysis_window_days = args.days
    if getattr(args, "min_cluster", None) is not None:
        settings.min_cluster_size = args.min_cluster
    if getattr(args, "min_samples", None) is not None:
        settings.min_samples = args.min_samples

    if args.command == "run":
        result = run_pipeline(
            settings,
            skip_ingest=args.skip_ingest,
            incremental=args.incremental,
            skip_map=args.skip_map,
            run_optimization=getattr(args, "optimize", False),
        )
        print("Pipeline complete")
        for key, value in result.items():
            print(f"{key}: {value}")
    elif args.command == "ingest":
        path = combine_raw_lbs_files(settings, incremental=args.incremental)
        print(f"Combined CSV: {path}")
    elif args.command == "map":
        path = generate_map(settings)
        print(f"Map: {path}")
    elif args.command == "patrol":
        import json, pandas as pd
        # Load medoids and incidents from existing outputs
        if not settings.medoids_json.exists():
            print(f"ERROR: medoids JSON not found at {settings.medoids_json}. Run pipeline first.")
            return
        if not settings.combined_csv.exists():
            print(f"ERROR: combined CSV not found at {settings.combined_csv}. Run ingest first.")
            return

        medoids = json.loads(settings.medoids_json.read_text(encoding="utf-8"))

        from .cleaning import load_and_clean_incidents
        from .geo import assign_boundaries
        incidents, _ = load_and_clean_incidents(settings.combined_csv, settings.analysis_window_days)
        incidents = assign_boundaries(incidents, settings)

        # Build final_locations from medoids that have real PS names
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
        if not medoid_rows:
            print("WARNING: No PS-level medoid locations found. Patrol routes require PS-level clustering.")
            return

        final_locations = pd.DataFrame(medoid_rows)
        patrol_df = generate_patrol_routes(
            settings, final_locations, incidents,
            max_km=args.max_km,
            max_min=args.max_min,
        )
        out = settings.output_dir / "patrol_routes.csv"
        print(f"Patrol routes: {out}")
        print(f"  Routes generated : {patrol_df['route_id'].nunique() if not patrol_df.empty else 0}")
        print(f"  Total stops      : {len(patrol_df)}")
        if not patrol_df.empty:
            invalid = patrol_df[patrol_df['valid'] == False]['route_id'].nunique()
            print(f"  Invalid routes   : {invalid}")


if __name__ == "__main__":
    main()