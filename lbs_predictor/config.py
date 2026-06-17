from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    project_root: Path = Path(__file__).resolve().parents[1]
    legacy_root: Path = Path(__file__).resolve().parents[2]

    min_cluster_size: int = 20
    min_samples: int = 10
    analysis_window_days: int | None = None

    earth_radius_km: float = 6371.0
    target_max_radius_km: float = 12.0
    road_factor: float = 2.3
    frv_avg_speed_kph: float = 30.0
    response_time_sample_points: int = 5
    use_osrm_driving_time: bool = False
    osrm_base_url: str = "http://router.project-osrm.org"
    mapbox_access_token: str = ""
    osrm_rate_limit_seconds: float = 0.05

    hotspot_color: str = "#ff6348"
    map_center_lat: float = 23.2599
    map_center_lon: float = 77.4126
    map_zoom: int = 7

    @property
    def raw_data_dir(self) -> Path:
        return self.project_root / "data" / "raw"

    @property
    def reference_dir(self) -> Path:
        return self.project_root / "data" / "reference"

    @property
    def processed_dir(self) -> Path:
        return self.project_root / "data" / "processed"

    @property
    def output_dir(self) -> Path:
        return self.project_root / "data" / "outputs"

    @property
    def checkpoint_dir(self) -> Path:
        return self.project_root / "checkpoints"

    @property
    def combined_csv(self) -> Path:
        return self.processed_dir / "final_combined_lbs_data.csv"

    @property
    def clustered_csv(self) -> Path:
        return self.output_dir / "clustered_data_fixed.csv"

    @property
    def medoids_json(self) -> Path:
        return self.output_dir / "medoids_fixed.json"

    @property
    def district_summaries_json(self) -> Path:
        return self.output_dir / "district_summaries_fixed.json"

    @property
    def response_summary_csv(self) -> Path:
        return self.output_dir / "response_times_summary_fixed.csv"

    @property
    def frv_placement_csv(self) -> Path:
        return self.output_dir / "frv_placement_distance_fixed.csv"

    @property
    def map_html(self) -> Path:
        return self.output_dir / "hotspot_map_fixed.html"

    @property
    def cleaning_audit_csv(self) -> Path:
        return self.output_dir / "data_cleaning_audit_fixed.csv"

    @property
    def checkpoint_file(self) -> Path:
        return self.checkpoint_dir / "processed_files.txt"

    @property
    def frv_master_csv(self) -> Path:
        return self.reference_dir / "Master_Unit_Export.csv"

    @property
    def district_mapping_csv(self) -> Path:
        return self.reference_dir / "District_Name_Mapping.csv"

    @property
    def districts_geojson(self) -> Path:
        return self.reference_dir / "District_1.json"

    @property
    def police_station_geojson(self) -> Path:
        return self.reference_dir / "PoliceStation.json"

    def ensure_dirs(self) -> None:
        for path in [self.processed_dir, self.output_dir, self.checkpoint_dir]:
            path.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    return Settings()
