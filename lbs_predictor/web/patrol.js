/* =========================================================
   LBS Predictor – Patrol Layer  |  patrol.js
   Depends on: Leaflet (window.L), map instance (window._lbsMap)
   and the payload (window._lbsMapPayload) set by map.js after it
   loads map_data.json. map_data.json is the single source of truth.
   ========================================================= */

"use strict";

// ── Patrol state ──────────────────────────────────────────
const patrol = {
  data:           null,       // legacy (patrol_data.json) — unused, kept for dead-code review
  layerGroup:     null,       // L.layerGroup for all patrol drawings
  allLayers:      [],         // registry of toggleable layers {_patrolType}
  activeAnims:    [],
  isAnimating:    false,
  routeCache:     {},
  showTerritory:  true,
  showWaypoints:  true,
  showRoutes:     true,
  showAnimation:  false,
};

const PATROL_MAX_WAYPOINTS_PER_FRV = 999;
const PATROL_MAX_ROUTE_KM = 15;
const PATROL_MAX_ROUTE_MIN = 60;

// ── Wait for map.js to expose the map, then boot ──────────
function waitForMap(cb) {
  if (window._lbsMap) { cb(window._lbsMap); return; }
  setTimeout(() => waitForMap(cb), 100);
}

async function initPatrol(map) {
  // map.js exposes window._lbsMapPayload (from map_data.json) before
  // window._lbsMap, so the payload is guaranteed to be ready here.
  const payload = window._lbsMapPayload || {};
  payload.patrolRoutes = payload.patrolRoutes || [];

  console.log({
      patrolRoutes: payload.patrolRoutes?.length,
      districts: [...new Set(payload.patrolRoutes.map(r => r.district))].length
  });

  patrol.layerGroup = L.layerGroup().addTo(map);

  populatePatrolDistricts();
  wirePatrolControls(map);
  wirePatrolFilterControls();

  if (!payload.patrolRoutes.length) {
    const stats = document.getElementById("patrol-route-stats");
    if (stats) {
      stats.innerHTML =
        '<span style="color:#dc2626">No patrol routes found in map_data.json.<br>' +
        'Run the pipeline with <code>--optimize</code> to generate them.</span>';
    }
  }
}

// ── map_data.json accessors ──────────────────────
function getPatrolPayload() {
  return window._lbsMapPayload || {};
}

function patrolRoutesAll() {
  const routes = getPatrolPayload().patrolRoutes;
  return Array.isArray(routes) ? routes : [];
}

// ── Populate district dropdown ────────────────────────────
function populatePatrolDistricts() {
  const sel     = document.getElementById("patrol-district");
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">— Select District —</option>';

  let districts = [...new Set(patrolRoutesAll().map(r => r.district).filter(Boolean))].sort();
  if (!districts.length) {
    districts = Object.keys(getPatrolPayload().districtPsMap || {}).sort();
  }
  districts.forEach(d => {
    const o = document.createElement("option");
    o.value = d; o.textContent = d;
    sel.appendChild(o);
  });
  if (current && districts.includes(current)) sel.value = current;
}

// ── Wire controls ─────────────────────────────────────────
function wirePatrolControls(map) {
  document.getElementById("patrol-district").onchange = () => onPatrolDistrict(map);
  document.getElementById("patrol-ps").onchange       = async () => await onPatrolPs(map);
}

function wirePatrolFilterControls() {
  ["toggle-patrol-territory", "toggle-waypoints", "toggle-patrol-routes", "toggle-patrol-animation"].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.onchange = () => applyPatrolToggles();
  });
}

function applyPatrolToggles() {
  patrol.showTerritory = document.getElementById("toggle-patrol-territory")?.checked ?? true;
  patrol.showWaypoints = document.getElementById("toggle-waypoints")?.checked ?? true;
  patrol.showRoutes = document.getElementById("toggle-patrol-routes")?.checked ?? true;
  patrol.showAnimation = document.getElementById("toggle-patrol-animation")?.checked ?? false;

  if (!patrol.layerGroup) return;
  // Iterate the registry (not the group) so layers toggled off can be toggled
  // back on — a removed layer is no longer enumerated by group.eachLayer().
  patrol.allLayers = patrol.allLayers.filter(layer => layer && layer._patrolType);
  patrol.allLayers.forEach(layer => {
    if (layer._patrolType === "territory") togglePatrolLayer(layer, patrol.showTerritory);
    else if (layer._patrolType === "waypoint") togglePatrolLayer(layer, patrol.showWaypoints);
    else if (layer._patrolType === "route") togglePatrolLayer(layer, patrol.showRoutes);
  });

  if (patrol.showAnimation) startPatrolAnimations();
  else stopPatrolAnimations();
}

function togglePatrolLayer(layer, visible) {
  const present = patrol.layerGroup.hasLayer(layer);
  if (visible && !present) patrol.layerGroup.addLayer(layer);
  if (!visible && present) patrol.layerGroup.removeLayer(layer);
}

// Register a toggleable patrol layer and add it respecting current toggles.
function addPatrolLayer(layer, type, isRoute) {
  layer._patrolType = type;
  if (isRoute) layer._isRoute = true;
  patrol.allLayers.push(layer);
  const visible =
    type === "territory" ? patrol.showTerritory :
    type === "waypoint"  ? patrol.showWaypoints :
    type === "route"     ? patrol.showRoutes : true;
  if (visible) patrol.layerGroup.addLayer(layer);
}

// ── District selected ─────────────────────────────────────
function onPatrolDistrict(map) {
  stopPatrolAnimations();
  clearPatrolLayers();

  const district = document.getElementById("patrol-district").value;
  const psSel    = document.getElementById("patrol-ps");
  const stats    = document.getElementById("patrol-dist-stats");
  const routeStats = document.getElementById("patrol-route-stats");

  psSel.innerHTML = '<option value="">— Select Police Station —</option>';
  psSel.disabled  = true;
  stats.textContent = "";
  routeStats.innerHTML = "";

  if (!district) return;

  const payload = getPatrolPayload();
  const routes  = patrolRoutesAll().filter(r => r.district === district);

  // District stats derived from backend patrol routes
  const psWithRoutes = [...new Set(routes.map(r => r.ps).filter(Boolean))];
  const totalLen = routes.reduce((s, r) => s + (Number(r.routeLengthKm) || 0), 0);
  stats.textContent =
    `Patrol routes: ${routes.length}  |  Police stations: ${psWithRoutes.length}  |  ` +
    `Total length: ${totalLen.toFixed(1)} km`;

  // Fit to district
  const db = (payload.districtBounds || {})[district];
  if (db) map.fitBounds(db, { padding: [20, 20] });

  // Draw PS boundaries for the district from psGeojson
  drawDistrictPsBoundaries(map, district);

  // Populate PS dropdown from PS that have backend routes (fallback to districtPsMap)
  let psNames = psWithRoutes.slice().sort();
  if (!psNames.length) {
    psNames = (payload.districtPsMap?.[district] || []).map(item => item.label).sort();
  }
  psNames.forEach(ps => {
    const o = document.createElement("option");
    o.value = ps; o.textContent = ps;
    psSel.appendChild(o);
  });
  psSel.disabled = psNames.length === 0;

  applyPatrolToggles();
}

// Draw all PS boundaries for a district straight from payload.psGeojson.
function drawDistrictPsBoundaries(map, district) {
  const fc = getPatrolPayload().psGeojson;
  if (!fc || !Array.isArray(fc.features)) return;
  fc.features
    .filter(f => f.properties && f.properties.map_district === district)
    .forEach(feature => {
      const psName = feature.properties.map_ps;
      const layer = L.geoJSON(feature, {
        style: { color: "#00d2d3", weight: 1.5, fillColor: "#00d2d3", fillOpacity: 0.03 },
      });
      layer.on("mouseover", () => layer.setStyle({ color: "#f97316", weight: 3, fillOpacity: 0.1 }));
      layer.on("mouseout",  () => layer.setStyle({ color: "#00d2d3", weight: 1.5, fillOpacity: 0.03 }));
      layer.on("click", () => {
        document.getElementById("patrol-ps").value = psName;
        onPatrolPs(map);
      });
      layer.bindTooltip(`<b>${esc(psName)} Police Station</b>`, { sticky: true });
      addPatrolLayer(layer, "territory");
    });
}

// ── PS selected ───────────────────────────────────────────
async function onPatrolPs(map) {
  stopPatrolAnimations();

  const district = document.getElementById("patrol-district").value;
  const ps       = document.getElementById("patrol-ps").value;
  const stats    = document.getElementById("patrol-route-stats");

  // Clear only route layers (keep PS boundaries)
  clearPatrolRouteLayers();
  stats.innerHTML = "";

  if (!district || !ps) return;

  const bounds = L.latLngBounds();

  // Highlight the selected PS boundary from psGeojson
  const fc = getPatrolPayload().psGeojson;
  if (fc && Array.isArray(fc.features)) {
    const feat = fc.features.find(f =>
      f.properties &&
      f.properties.map_district === district &&
      normalizeName(f.properties.map_ps) === normalizeName(ps)
    );
    if (feat) {
      const psLayer = L.geoJSON(feat, {
        style: { color: "#00d2d3", weight: 3, fillColor: "#00d2d3", fillOpacity: 0.1, dashArray: "4,4" },
      });
      addPatrolLayer(psLayer, "territory", true);
      try { bounds.extend(psLayer.getBounds()); } catch (_e) { /* empty bounds */ }
    }
  }

  // Render backend-generated patrol routes for this PS
  const routes = patrolRoutesAll().filter(r =>
    r.district === district && normalizeName(r.ps) === normalizeName(ps)
  );
  const summary = renderBackendRoutes(routes, bounds);
  stats.innerHTML = summary || `No backend patrol routes found for <b>${esc(ps)}</b>.`;

  applyPatrolToggles();
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [30, 30] });
}

// Render backend patrol routes (polyline + waypoint markers) into the patrol
// layer group. Returns an HTML summary string, or null when nothing was drawn.
function renderBackendRoutes(routes, bounds) {
  let totalDistance = 0, totalDuration = 0, routeCount = 0, waypointCount = 0;

  routes.forEach(route => {
    const info = routeToRouteInfo(route);
    if (!info || info.coords.length < 2) return;
    const coords = info.coords;
    const distanceKm = Number(route.routeLengthKm) || distanceMeters(coords) / 1000;
    const durationMin = Number(route.durationMin) || Math.round(distanceKm * 2.5);

    const routeLine = L.polyline(coords, { color: "#16a34a", weight: 3, opacity: 0.8 });
    routeLine.bindPopup(buildBackendRoutePopup(route, distanceKm, durationMin));
    addPatrolLayer(routeLine, "route", true);
    if (bounds) bounds.extend(routeLine.getBounds());

    (route.points || []).forEach(pt => {
      if (String(pt.stopType || "").toUpperCase() !== "WAYPOINT") return;
      waypointCount += 1;
      const marker = L.circleMarker([pt.lat, pt.lon], {
        radius: 4, color: "#ef5555", fillColor: "#ffffff", fillOpacity: 0.9, weight: 1.5,
      });
      marker.bindTooltip("Stop #" + (pt.stopSeq != null ? pt.stopSeq : ""));
      marker.bindPopup(buildWaypointPopup(route, pt));
      addPatrolLayer(marker, "waypoint", true);
      if (bounds) bounds.extend([pt.lat, pt.lon]);
    });

    totalDistance += distanceKm;
    totalDuration += durationMin;
    routeCount += 1;
  });

  if (!routeCount) return null;
  const durationMinutes = totalDuration || Math.round(totalDistance * 2.5);
  return `<b>Backend patrol routes</b><br>` +
    `FRV routes: <b>${routeCount}</b><br>` +
    `Waypoint stops: <b>${waypointCount}</b><br>` +
    `Total route length: <b>${totalDistance.toFixed(1)} km</b><br>` +
    `Estimated patrol time: <b>${durationMinutes} min</b>`;
}

function buildWaypointPopup(route, pt) {
  return `<div style="font-family:Segoe UI,sans-serif;color:#17212b;min-width:220px">
    <h4 style="margin:0 0 5px;color:#1e90ff">Waypoint Stop</h4>
    <table style="font-size:12px;width:100%">
      <tr><td style="color:#66717d">FRV:</td><td style="text-align:right"><b>${esc(route.frvId || "N/A")}</b></td></tr>
      <tr><td style="color:#66717d">Police Station:</td><td style="text-align:right"><b>${esc(route.ps || "N/A")}</b></td></tr>
      <tr><td style="color:#66717d">Stop #:</td><td style="text-align:right"><b>${esc(pt.stopSeq != null ? pt.stopSeq : "")}</b></td></tr>
      <tr><td style="color:#66717d">Incident weight:</td><td style="text-align:right"><b>${Number(pt.incidentWeight || 0).toFixed(2)}</b></td></tr>
      <tr><td style="color:#66717d">Cumulative dist:</td><td style="text-align:right"><b>${Number(pt.cumulativeDistKm || 0).toFixed(2)} km</b></td></tr>
    </table></div>`;
}

// ── Shared helpers (used by the active map_data.json flow) ─────────────────
function normalizeName(value) {
  return String(value || "").trim().toLowerCase();
}

function routeToRouteInfo(route) {
  if (!route || !Array.isArray(route.points)) return null;
  const coords = route.points
    .map(point => [Number(point.lat), Number(point.lon)])
    .filter(coord => Number.isFinite(coord[0]) && Number.isFinite(coord[1]));
  if (coords.length < 2) return null;
  return {
    coords,
    durationMin: Number(route.durationMin) || 0,
    visitedWaypointsCount: Number(route.waypointCount) || Math.max(0, coords.length - 2),
  };
}

function buildBackendRoutePopup(route, distanceKm, durationMin) {
  return `<div style="font-family:Segoe UI,sans-serif;color:#17212b;min-width:240px">
    <h4 style="margin:0 0 6px;color:#16a34a">Patrol Route ${esc(route.routeId || "")}</h4>
    <table style="font-size:12px;width:100%">
      <tr><td style="color:#66717d">FRV:</td><td style="text-align:right"><b>${esc(route.frvId || "N/A")}</b></td></tr>
      <tr><td style="color:#66717d">Police Station:</td><td style="text-align:right"><b>${esc(route.ps || "N/A")}</b></td></tr>
      <tr><td style="color:#66717d">Route length:</td><td style="text-align:right"><b>${distanceKm.toFixed(1)} km</b></td></tr>
      <tr><td style="color:#66717d">Patrol time:</td><td style="text-align:right"><b>${Math.round(durationMin)} min</b></td></tr>
      <tr><td style="color:#66717d">Waypoints:</td><td style="text-align:right"><b>${Number(route.waypointCount || 0)}</b></td></tr>
      <tr><td style="color:#66717d">Coverage:</td><td style="text-align:right"><b>${Number(route.coverage || 0).toFixed(1)}%</b></td></tr>
    </table></div>`;
}

function distanceMeters(routeCoords) {
  let total = 0;
  for (let i = 1; i < routeCoords.length; i += 1) {
    const a = routeCoords[i - 1];
    const b = routeCoords[i];
    const dLat = (b[0] - a[0]) * Math.PI / 180;
    const dLon = (b[1] - a[1]) * Math.PI / 180;
    const lat1 = a[0] * Math.PI / 180;
    const lat2 = b[0] * Math.PI / 180;
    const sinLat = Math.sin(dLat / 2);
    const sinLon = Math.sin(dLon / 2);
    const c = 2 * Math.atan2(
      Math.sqrt(sinLat * sinLat + Math.cos(lat1) * Math.cos(lat2) * sinLon * sinLon),
      Math.sqrt(1 - (sinLat * sinLat + Math.cos(lat1) * Math.cos(lat2) * sinLon * sinLon))
    );
    total += 6371000 * c;
  }
  return total;
}

/* ============================================================================
   LEGACY (patrol_data.json scenario workflow) — UNUSED / DEAD-CODE CANDIDATES
   ----------------------------------------------------------------------------
   The functions below built patrol routes in JavaScript from the old
   patrol_data.json scenario structure. They are no longer called by the active
   map_data.json flow above and are kept only for review before removal.
   See docs/VISUALIZATION_REFACTOR_ANALYSIS.md (section 5 / dead-code list).
   ============================================================================ */

function buildFrvPatrolAssignments(district, ps, psData) {
  // Build assignments per FRV: assign nearest waypoints to nearest FRV until cap
  const waypointRefs = collectWaypointRefs(psData);
  const baseFrvs = getPatrolFrvsForPs(district, ps);
  const requiredFrvCount = Math.max(
    baseFrvs.length,
    waypointRefs.length ? Math.ceil(waypointRefs.length / PATROL_MAX_WAYPOINTS_PER_FRV) : 1
  );
  const frvs = expandPatrolFrvs(baseFrvs, requiredFrvCount, ps);
  const assignments = frvs.map(frv => ({ frv, zones: [], waypoints: [], _zoneMap: {} }));

  if (!assignments.length) return [];

  // If there are explicit waypoint refs, assign each waypoint to the nearest FRV that has capacity
  if (waypointRefs.length) {
    // copy refs
    const remaining =
      waypointRefs
      .slice()
      .sort(
          (a,b)=>
              (b.wp.weight || 1)
              -
              (a.wp.weight || 1)
      );
    // Precompute FRV coords
    const frvCoords = assignments.map(a => [Number(a.frv.lat), Number(a.frv.lon)]);

    // For each waypoint, find nearest FRV that still has capacity
    while (remaining.length) {
      const ref = remaining.shift();
      // compute distances to FRVs
      let bestIdx = -1, bestD = Infinity;
      for (let i = 0; i < assignments.length; i++) {
        if (assignments[i].waypoints.length >= PATROL_MAX_WAYPOINTS_PER_FRV) continue;
        const fc = frvCoords[i];
        const d = Number.isFinite(fc[0]) && Number.isFinite(fc[1])
          ? haversineKm(fc, [Number(ref.wp.lat), Number(ref.wp.lon)])
          : Infinity;
        if (d < bestD) { bestD = d; bestIdx = i; }
      }
      // if no FRV has capacity assign round-robin
      if (bestIdx === -1) bestIdx = remaining.length % assignments.length;
      addWaypointToAssignment(assignments[bestIdx], ref.zoneId, ref.zoneData, ref.wp);
    }
  } else {
    // No explicit waypoints: assign zones round-robin
    Object.entries(psData || {}).forEach(([zoneId, zoneData], idx) => {
      const assignment = assignments[idx % assignments.length];
      assignment.zones.push({ zoneId, zoneData });
    });
  }

  // Order waypoints per FRV using nearest-neighbor starting at FRV
  assignments.forEach(assignment => {
    assignment.waypoints = buildBudgetedPatrolRoute(
        assignment.frv,
        assignment.waypoints,
        PATROL_MAX_ROUTE_KM
    );

    delete assignment._zoneMap;
  });

  return assignments.filter(assignment => assignment.zones.length || assignment.waypoints.length);
}

function collectWaypointRefs(psData) {
  const refs = [];
  Object.entries(psData || {}).forEach(([zoneId, zoneData]) => {
    (zoneData.waypoints || []).forEach((wp, idx) => {
      refs.push({ zoneId, zoneData, wp, idx });
    });
  });
  return refs;
}

function expandPatrolFrvs(baseFrvs, requiredCount, ps) {
  if (!baseFrvs.length) return [];
  const frvs = [];
  for (let i = 0; i < requiredCount; i += 1) {
    const source = baseFrvs[i % baseFrvs.length];
    frvs.push({
      ...source,
      frvId: i < baseFrvs.length
        ? source.frvId
        : `${source.frvId || ps + "-FRV"}-${String(i + 1).padStart(2, "0")}`,
      sourceFrvId: source.frvId,
      routeSlot: i + 1,
    });
  }
  return frvs;
}

function addWaypointToAssignment(assignment, zoneId, zoneData, wp) {
    

    if (
        assignment.frv.ps &&
        wp.ps &&
        assignment.frv.ps !== wp.ps
    ) {
        return;
    }

    if (!assignment._zoneMap[zoneId]) {
        const assignedZone = {
            ...zoneData,
            waypoints: []
        };

        assignment._zoneMap[zoneId] = assignedZone;

        assignment.zones.push({
            zoneId,
            zoneData: assignedZone
        });
    }

    assignment._zoneMap[zoneId].waypoints.push(wp);
    assignment.waypoints.push(wp);
}

function orderWaypointsNearestNeighbor(frv, waypoints) {
  const ordered = [];
  const remaining = waypoints.slice();
  let cursor = [Number(frv.lat), Number(frv.lon)];
  while (remaining.length) {
    const idx = nearestWaypointIndex(cursor, remaining.map(wp => ({ wp })));
    const wp = remaining.splice(idx, 1)[0];
    ordered.push(wp);
    cursor = [Number(wp.lat), Number(wp.lon)];
  }
  return ordered;
}
function buildBudgetedPatrolRoute(
    frv,
    waypoints,
    maxKm = 15
){

    if(!waypoints || !waypoints.length){
        return [];
    }

    const selected = [];

    const sorted =
      [...waypoints]
      .sort((a,b)=>{

          const scoreA =
              (a.weight || 1) +
              (a.incidents || 0);

          const scoreB =
              (b.weight || 1) +
              (b.incidents || 0);

          return scoreB - scoreA;
      });

    for(const wp of sorted){

        const test =
            [...selected, wp];

        const routeKm =
            estimateRouteLength(
                frv,
                test
            );

        if(routeKm <= maxKm){
            selected.push(wp);
        }
    }

    return orderWaypointsNearestNeighbor(
        frv,
        selected
    );
}
function estimateRouteLength(
    frv,
    waypoints
){

    if(!waypoints.length){
        return 0;
    }

    let total = 0;

    let prev = [
        Number(frv.lat),
        Number(frv.lon)
    ];

    for(const wp of waypoints){

        const curr = [
            Number(wp.lat),
            Number(wp.lon)
        ];

        total += haversineKm(
            prev,
            curr
        );

        prev = curr;
    }

    total += haversineKm(
        prev,
        [
            Number(frv.lat),
            Number(frv.lon)
        ]
    );

    return total * 1.3;
}

function nearestWaypointIndex(cursor, refs) {
  let bestIdx = 0;
  let bestDistance = Infinity;
  refs.forEach((ref, idx) => {
    const d = haversineKm(cursor, [Number(ref.wp.lat), Number(ref.wp.lon)]);
    if (d < bestDistance) {
      bestDistance = d;
      bestIdx = idx;
    }
  });
  return bestIdx;
}

function getPatrolFrvsForPs(district, ps) {
  const mapPayload = window._lbsMapPayload || {};
  const deployment = window._lbsDeploymentMode === "optimized" && (mapPayload.optimizedFrvPoints || []).length
    ? "optimized"
    : "current";
  const points = deployment === "optimized"
    ? (mapPayload.optimizedFrvPoints || [])
    : (mapPayload.currentFrvPoints || mapPayload.frvPoints || []);
  const targetKey = `${district}||${ps}`;
  const targetPs = normalizeName(ps);
  const frvs = points
    .filter(pt => pt.psKey === targetKey || (pt.district === district && normalizeName(pt.ps) === targetPs))
    .map((pt, idx) => ({
      ...pt,
      frvId: pt.frvId || pt.FRV_ID || pt.frv_id || `${ps}-FRV-${idx + 1}`,
    }))
    .sort((a, b) => String(a.frvId).localeCompare(String(b.frvId)));

  if (frvs.length) return frvs;

  const psInfo = ((patrol.data.psBounds[district] || {})[ps]);
  const centroid = psInfo?.centroid || [null, null];
  return [{
    frvId: `${ps}-FRV-01`,
    lat: centroid[0],
    lon: centroid[1],
    district,
    ps,
    psKey: targetKey,
  }];
}

async function addPatrolRoutes(assignments) {
  let totalDistance = 0;
  let totalDuration = 0;
  let routeCount = 0;
  let assignedWaypointCount = 0;
  let visitedWaypointCount = 0;
  const bounds = L.latLngBounds();

  const routes = getBackendPatrolRoutesForAssignments(assignments);
  routes.forEach(route => {
    const routeInfo = routeToRouteInfo(route);
    if (!routeInfo || !routeInfo.coords || routeInfo.coords.length < 2) return;
    const routeCoords = routeInfo.coords;
    const distanceKm = Number(route.routeLengthKm) || distanceMeters(routeCoords) / 1000;
    const durationMin = Number(route.durationMin) || Math.round(distanceKm * 2.5);
    const visited = Number(route.waypointCount) || routeInfo.visitedWaypointsCount || 0;
    assignedWaypointCount += visited;
    visitedWaypointCount += visited;

    const routeLine = L.polyline(routeCoords, {
      color: "#16a34a",
      weight: 3,
      opacity: 0.75,
    });
    routeLine._isRoute = true;
    routeLine._patrolType = "route";
    routeLine.bindPopup(buildBackendRoutePopup(route, distanceKm, durationMin));
    patrol.layerGroup.addLayer(routeLine);
    bounds.extend(routeLine.getBounds());

    totalDistance += distanceKm;
    totalDuration += durationMin;
    routeCount += 1;
  });

  if (!routeCount) return null;

  const durationMinutes = totalDuration || Math.round(totalDistance * 2.5);
  return {
    bounds,
    summary:
      `<b>Backend patrol routes</b><br>` +
      `FRV routes: <b>${routeCount}</b><br>` +
      `Waypoints visited: <b>${visitedWaypointCount}/${assignedWaypointCount}</b><br>` +
      `↳ Total route length: <b>${totalDistance.toFixed(1)} km</b><br>` +
      `↳ Estimated patrol time: <b>${durationMinutes} min</b>`,
  };
}

async function fetchPatrolRoute(waypoints, frv) {
  const route = getBackendPatrolRoute(frv);
  if (!route) return null;
  const cacheKey = `backend|${route.routeId || ""}|${route.frvId || ""}`;
  if (patrol.routeCache[cacheKey]) return patrol.routeCache[cacheKey];
  patrol.routeCache[cacheKey] = routeToRouteInfo(route);
  return patrol.routeCache[cacheKey];
}

function getBackendPatrolRoutesForAssignments(assignments) {
  const payload = window._lbsMapPayload || {};
  const allRoutes = payload.patrolRoutes || [];
  const seen = new Set();
  const routes = [];
  assignments.forEach(assignment => {
    const frv = assignment.frv || {};
    allRoutes.forEach(route => {
      const sameFrv = route.frvId && frv.frvId && String(route.frvId) === String(frv.frvId);
      const samePs = route.ps && frv.ps && normalizeName(route.ps) === normalizeName(frv.ps);
      const sameDistrict = !route.district || !frv.district || route.district === frv.district;
      if ((sameFrv || (samePs && sameDistrict)) && !seen.has(route.routeId)) {
        seen.add(route.routeId);
        routes.push(route);
      }
    });
  });
  return routes;
}

function getBackendPatrolRoute(frv) {
  return getBackendPatrolRoutesForAssignments([{ frv: frv || {} }])[0] || null;
}

function haversineKm(a, b) {
  return distanceMeters([a, b]) / 1000;
}

// ── Animation ─────────────────────────────────────────────
function togglePatrolAnimation() {
  
  if (patrol.isAnimating) {
    stopPatrolAnimations();
    btn.innerHTML = "▶ Start Presentation Mode";
  } else {
    startPatrolAnimations();
    btn.innerHTML = "⏹ Stop Animation";
    patrol.isAnimating = true;
  }
}

function startPatrolAnimations() {
  const district = document.getElementById("patrol-district").value;
  const ps       = document.getElementById("patrol-ps").value;
  if (!district || !ps) return;

  const routes = patrolRoutesAll().filter(r =>
    r.district === district && normalizeName(r.ps) === normalizeName(ps)
  );
  animateRoutes(routes);
}

// Animate one patrol vehicle along each backend route's coordinate path.
function animateRoutes(routes) {
  const carIcon = L.divIcon({
    html: '<div style="font-size:22px;text-shadow:0 0 4px #000;line-height:22px">🚓</div>',
    className: "", iconSize: [22, 22], iconAnchor: [11, 11],
  });

  routes.forEach(route => {
    const info = routeToRouteInfo(route);
    if (!info || info.coords.length < 2) return;
    const coords = info.coords;
    const marker = L.marker(coords[0], { icon: carIcon }).addTo(patrol.layerGroup);
    const anim = animateAlongRoute(marker, coords, {});
    patrol.activeAnims.push({ cancel: anim.cancel, marker });
  });
  patrol.isAnimating = patrol.activeAnims.length > 0;
}

// Smooth animation helper: moves a marker along coords with pauses at waypoint indices.
function animateAlongRoute(marker, coords, wpIndices) {
  let stopped = false;
  const speedMs = 12.5; // meters per second (~45 km/h) — conservative

  // Precompute cumulative distances
  const segLengths = [];
  let total = 0;
  for (let i = 1; i < coords.length; i++) {
    const a = coords[i-1], b = coords[i];
    const d = distanceMeters([a, b]);
    segLengths.push(d);
    total += d;
  }

  let seg = 0;
  let segPos = 0; // meters along current segment
  let lastTs = null;

  function step(ts) {
    if (stopped) return;
    if (!lastTs) lastTs = ts;
    const dt = (ts - lastTs) / 1000; // seconds
    lastTs = ts;

    const move = speedMs * dt;
    segPos += move;
    while (seg < segLengths.length && segPos > segLengths[seg]) {
      segPos -= segLengths[seg];
      seg += 1;
    }
    if (seg >= segLengths.length) {
      // loop
      seg = 0; segPos = 0;
    }

    // interpolate position
    const a = coords[seg];
    const b = coords[seg+1] || coords[seg];
    const frac = segLengths[seg] > 0 ? (segPos / segLengths[seg]) : 0;
    const lat = a[0] + (b[0]-a[0]) * frac;
    const lon = a[1] + (b[1]-a[1]) * frac;
    marker.setLatLng([lat, lon]);

    // Pause at waypoint indices if near
    const idx = seg + (frac > 0.5 ? 1 : 0);
    const wp = wpIndices[idx];
    if (wp) {
      // Pause by delaying next frame
      const dwellSec = Math.max(wp.dwell_m || 0, wp.dwell_e || 0, wp.dwell_n || 0) / 60; // minutes->hours? wp values are minutes, convert to seconds below
      const dwellMs = Math.max(0, Math.round(dwellSec * 60 * 1000));
      if (dwellMs > 0) {
        // schedule resume after dwell
        setTimeout(() => { if (!stopped) requestAnimationFrame(step); }, dwellMs);
        return;
      }
    }

    requestAnimationFrame(step);
  }

  requestAnimationFrame(step);
  return { cancel() { stopped = true; } };
}

// Show the backend patrol route(s) for a specific FRV when its marker is clicked.
async function showFrv(frvId, pt, deployment) {
  clearPatrolRouteLayers();
  stopPatrolAnimations();
  if (!window._lbsMap) return;

  const statsEl = document.getElementById("patrol-route-stats");
  const routes = patrolRoutesAll().filter(r => String(r.frvId) === String(frvId));

  if (!routes.length) {
    if (statsEl) statsEl.innerHTML = `No patrol route found for <b>${esc(frvId)}</b>.`;
    return;
  }

  const bounds = L.latLngBounds();
  const summary = renderBackendRoutes(routes, bounds);
  if (statsEl) statsEl.innerHTML = `<b>FRV ${esc(frvId)}</b><br>` + (summary || "");

  applyPatrolToggles();
  if (patrol.showAnimation) animateRoutes(routes);
  if (bounds.isValid()) window._lbsMap.fitBounds(bounds, { padding: [28, 28] });
}

// Expose to map.js click handlers
window._showFrv = showFrv;

function stopPatrolAnimations() {
  patrol.activeAnims.forEach(a => {
    if (a.interval) try { clearInterval(a.interval); } catch(e){}
    if (a.cancel) try { a.cancel(); } catch(e){}
    try { patrol.layerGroup && patrol.layerGroup.removeLayer(a.marker); } catch(e){}
  });
  patrol.activeAnims = [];
  patrol.isAnimating = false;
}

// ── Layer helpers ─────────────────────────────────────────
function clearPatrolLayers() {
  stopPatrolAnimations();
  if (patrol.layerGroup) patrol.layerGroup.clearLayers();
  patrol.allLayers = [];
}

function clearPatrolRouteLayers() {
  // Remove only _isRoute layers, keeping PS boundary previews
  if (!patrol.layerGroup) return;
  patrol.layerGroup.eachLayer(l => {
    if (l._isRoute) patrol.layerGroup.removeLayer(l);
  });
  patrol.allLayers = patrol.allLayers.filter(l => !l._isRoute);
}

// ── Utility ───────────────────────────────────────────────
function esc(v) {
  return String(v == null ? "" : v)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

// ── Boot ──────────────────────────────────────────────────
waitForMap(initPatrol);
