"""Tests for budget-constrained, coverage-maximising patrol routing."""

import pandas as pd

from lbs_predictor.config import Settings
from lbs_predictor.patrol_routing import generate_patrol_routes


def _make_incidents() -> pd.DataFrame:
    """Synthetic incidents for one PS: a few crime cells of varying intensity."""
    rows = []
    # (lat, lon, count) — counts encode crime intensity per ~100 m cell.
    cells = [
        (23.000, 77.000, 40),   # very hot, right next to base
        (23.010, 77.010, 30),   # hot, close
        (23.020, 77.015, 20),   # medium, a bit farther
        (23.035, 77.030, 12),   # cooler, farther
        (23.060, 77.060, 6),    # far, low weight
        (23.090, 77.090, 3),    # very far, low weight
    ]
    for lat, lon, count in cells:
        for _ in range(count):
            rows.append({"ps": "TestPS", "latitude": lat, "longitude": lon})
    return pd.DataFrame(rows)


def _final_locations() -> pd.DataFrame:
    return pd.DataFrame([
        {"frv_id": "FRV-1", "ps": "TestPS", "latitude": 23.000, "longitude": 77.000},
    ])


def _settings(tmp_path) -> Settings:
    s = Settings(project_root=tmp_path)
    s.ensure_dirs()
    return s


def test_every_route_within_budget(tmp_path):
    settings = _settings(tmp_path)
    df = generate_patrol_routes(settings, _final_locations(), _make_incidents())
    assert not df.empty
    per_route = df.drop_duplicates("route_id")
    assert (per_route["total_route_km"] <= settings.patrol_max_km + 1e-6).all()
    assert per_route["valid"].all()


def test_crime_coefficient_in_unit_range(tmp_path):
    settings = _settings(tmp_path)
    df = generate_patrol_routes(settings, _final_locations(), _make_incidents())
    assert (df["crime_coefficient"] >= 0).all()
    assert (df["crime_coefficient"] <= 1).all()
    # The busiest visited cell should reach the top of the range.
    waypoints = df[df["stop_type"] == "WAYPOINT"]
    assert waypoints["crime_coefficient"].max() == 1.0


def test_larger_budget_covers_at_least_as_much(tmp_path):
    """Coverage should be monotonic non-decreasing in the route budget."""
    settings = _settings(tmp_path)
    incidents = _make_incidents()

    small = generate_patrol_routes(settings, _final_locations(), incidents, max_km=5.0)
    large = generate_patrol_routes(settings, _final_locations(), incidents, max_km=20.0)

    cov_small = small.drop_duplicates("route_id")["coverage_pct"].max()
    cov_large = large.drop_duplicates("route_id")["coverage_pct"].max()
    assert cov_large >= cov_small
    # With the full 20 km budget the route should cover a meaningful share.
    assert cov_large >= 50.0


def test_prioritises_high_crime_cells(tmp_path):
    """The hottest cells must be visited before low-weight far ones."""
    settings = _settings(tmp_path)
    df = generate_patrol_routes(settings, _final_locations(), _make_incidents())
    visited = df[df["stop_type"] == "WAYPOINT"]
    # The single highest-weight cell (40 incidents) must be on a route.
    assert visited["incident_weight"].max() == 40
