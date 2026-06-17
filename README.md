# Production LBS Predictor

Production-grade Location Based Services (LBS) analytics and response optimization platform for incident hotspot detection, FRV deployment planning, patrol waypoint generation, and response-time optimization.

---

## Overview

The system processes incident location data, identifies spatial incident patterns, optimizes First Response Vehicle (FRV) deployment, generates patrol coverage zones, and produces interactive geospatial dashboards for operational planning.

### Core Features

- Incident data ingestion and preprocessing
- Coordinate validation and geospatial cleaning
- District and Police Station jurisdiction mapping
- HDBSCAN-based hotspot detection
- Adaptive hotspot refinement and cluster optimization
- FRV deployment planning and allocation
- Medoid-based deployment location selection
- Response-time estimation using road-network routing
- Patrol waypoint generation and patrol-zone planning
- Interactive geospatial visualization and analytics
- Automated reporting through CSV, JSON, and HTML outputs

---

## Processing Pipeline

1. Ingest incident location data.
2. Parse and extract geospatial coordinates.
3. Clean and validate geographic records.
4. Assign incidents to districts and police-station jurisdictions.
5. Detect incident hotspots using HDBSCAN clustering.
6. Optimize hotspot coverage and resource allocation.
7. Select representative deployment locations using medoid analysis.
8. Estimate response times using routing and distance calculations.
9. Generate patrol zones and waypoint recommendations.
10. Produce reports, analytics datasets, and interactive maps.

---

## Project Structure

```text
production_lbs_predictor/
│
├── checkpoints/
│
├── data/
│   ├── raw/
│   ├── processed/
│   ├── outputs/
│   ├── patrol_waypoints/
│   └── reference/
│
├── lbs_predictor/
│   ├── __init__.py
│   ├── cli.py
│   ├── cleaning.py
│   ├── clustering.py
│   ├── deployment.py
│   ├── mapping.py
│   └── ...
│
├── requirements.txt
├── setup.py
└── README.md
```

---

## Reference Data

The platform uses administrative and operational reference datasets stored in:

```text
data/reference/
├── District_1.json
├── PoliceStation.json
├── District_Name_Mapping.csv
└── Master_Unit_Export.csv
```

### Reference Files

| File | Purpose |
|--------|----------|
| District_1.json | District boundaries |
| PoliceStation.json | Police station boundaries |
| District_Name_Mapping.csv | District name standardization |
| Master_Unit_Export.csv | Police unit and station reference data |

---

## Generated Outputs

The system automatically creates the following output datasets and visualizations:

```text
data/processed/
data/outputs/
data/patrol_waypoints/
checkpoints/
```

### Output Artifacts

- Clustered incident datasets
- District-wise hotspot summaries
- FRV deployment recommendations
- Response-time analytics
- Patrol waypoint datasets
- Patrol-zone summaries
- Interactive hotspot maps
- Interactive patrol maps
- CSV and JSON reports

---

## Patrol Planning Outputs

Generated patrol planning files include:

```text
data/patrol_waypoints/

patrol_waypoints_5m.csv
patrol_waypoints_10m.csv
patrol_waypoints_1200.csv
patrol_waypoints_actual.csv

patrol_zones_summary_5m.csv
patrol_zones_summary_10m.csv
patrol_zones_summary_1200.csv
patrol_zones_summary_actual.csv

patrol_waypoint_map.html
```

These files support patrol-zone analysis, waypoint generation, patrol coverage visualization, and operational planning.

---

## Running the Pipeline

Run the complete workflow:

```powershell
python -m lbs_predictor.cli run
```

Run while skipping ingestion:

```powershell
python -m lbs_predictor.cli run --skip-ingest
```

---

## Useful Commands

```powershell
python -m lbs_predictor.cli run --days 30

python -m lbs_predictor.cli run --skip-ingest --skip-map

python -m lbs_predictor.cli run --min-cluster 30 --min-samples 15

python -m lbs_predictor.cli ingest

python -m lbs_predictor.cli map
```

---

## Installation

Install dependencies:

```powershell
pip install -r requirements.txt
```

Install the package in editable mode:

```powershell
pip install -e .
```

---

## Technology Stack

- Python
- Pandas
- GeoPandas
- Folium
- HDBSCAN
- Scikit-Learn
- Shapely
- NumPy
- OSRM Routing Engine

---

## Generated Visualizations

### Hotspot Map

Interactive dashboard showing:

- Incident heatmaps
- Hotspot zones
- District boundaries
- Police station boundaries
- FRV deployment locations
- Response-time analytics
- Area Explorer controls

### Patrol Map

Interactive dashboard showing:

- Patrol zones
- Waypoint stops
- Patrol coverage areas
- District and police station filters
- Patrol summaries
- Zone-level analytics
- Operational planning views

---

## Use Cases

- Police resource deployment planning
- Emergency response optimization
- Patrol route planning
- Patrol waypoint generation
- Hotspot identification and monitoring
- District-level operational analytics
- Geospatial decision support systems
- Public safety resource allocation

---
