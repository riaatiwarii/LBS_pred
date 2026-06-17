/* =========================================================
  LBS Predictor - Clean Map  |  map.js
  ========================================================= */

"use strict";

// -- State -------------------------------------------------
const state = {
  district:              "",
  psKey:                 "",
  deploymentMode:        "current",
  showDistricts:         true,
  showPs:                true,
  showPsMarkers:         true,
  showFrv:               true,
  showCurrentFrvs:       true,
  showOptimizedFrvs:     false,
  showTransfers:         false,
  showHeatmapCurrent:    false,
  showHeatmapOptimized:  false,
};

const layers = {
  districts:        [],
  psBoundaries:     [],
  psMarkers:        [],
  frvMarkers:       [],
  currentFrvMarkers: [],
  optimizedFrvMarkers: [],
  transferLines:    [],
  heatmapMarkers:   [],
  currentFrvGroup:  null,
  optimizedFrvGroup:null,
  transferGroup:    null,
  heatmapGroup:     null,
};

let map     = null;
let payload = null;

// -- Bootstrap ---------------------------------------------
async function init() {
  try {
    payload = await loadMapPayload();
    window._lbsMapPayload = payload;
  } catch (err) {
    showError("Could not load map_data.json: " + err.message);
    return;
  }

  map = L.map("map", {
    preferCanvas: true,
    zoomControl: true
  });

  window._lbsMap = map;
  window._lbsDeploymentMode = state.deploymentMode;

  payload.currentFrvPoints = payload.currentFrvPoints || payload.frvPoints || [];
  payload.psLookup = Object.fromEntries(
    (payload.psPoints || []).map(pt => [normalizePsKey(pt.district, pt.ps), pt])
  );
  normalizeFrvResponseFields(payload.currentFrvPoints);

  payload.transferRows = payload.transferRows || [];
  payload.resimulationRows = payload.resimulationRows || [];
  payload.patrolRoutes = payload.patrolRoutes || [];
  payload.optimizedFrvPoints = payload.optimizedFrvPoints || deriveOptimizedFrvPoints(payload);
  normalizeFrvResponseFields(payload.optimizedFrvPoints);
  payload.transferLines = buildTransferLines(payload.transferRows, payload.psLookup);
  const hasOptimized = payload.optimizedFrvPoints.length > 0;

  const optimizedCheckbox = document.getElementById("toggle-optimized-frvs");
  const optimizedRadio = document.querySelector('input[name="deployment-mode"][value="optimized"]');
  const optimizedHeatmap = document.getElementById("toggle-heatmap-optimized");
  if (optimizedCheckbox) optimizedCheckbox.disabled = !hasOptimized;
  if (optimizedRadio) optimizedRadio.disabled = !hasOptimized;
  if (optimizedHeatmap) optimizedHeatmap.disabled = !hasOptimized;
  updateDynamicLegendItems();

  // Base tile layers
  const carto = L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
    { attribution: "(c) CartoDB", subdomains: "abcd", maxZoom: 19 }
  ).addTo(map);

  const osm = L.tileLayer(
    "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    { attribution: "(c) OpenStreetMap contributors", maxZoom: 19 }
  );

  const satellite = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { attribution: "(c) Esri", maxZoom: 19 }
  );

  L.control.layers(
    { "Light (default)": carto, "Street Map": osm, "Satellite": satellite },
    {},
    { position: "topright" }
  ).addTo(map);
  L.control.scale().addTo(map);

  // Performance: create dedicated layer groups for quick add/remove
  layers.currentFrvGroup = L.layerGroup().addTo(map);
  layers.optimizedFrvGroup = L.layerGroup().addTo(map);
  layers.transferGroup = L.layerGroup().addTo(map);
  layers.heatmapGroup = L.layerGroup().addTo(map);

  // Set initial view
  if (payload.allBounds) {
    map.fitBounds(payload.allBounds, { padding: [18, 18] });
  } else {
    map.setView(payload.defaultCenter, payload.defaultZoom);
  }

  addGeoJsonLayers();
  addPointLayers();
  populateDistricts();
  populatePoliceStations("");
  wireControls();
  applyFilters();
  updateDeploymentNote();

  hideLoading();
}

// -- GeoJSON layers ----------------------------------------
function addGeoJsonLayers() {
  L.geoJSON(payload.districtGeojson, {
    style: districtStyle,
    onEachFeature(feature, layer) {
      layer._kind     = "district";
      layer._district = feature.properties.map_district;
      layer.bindTooltip(feature.properties.map_district);
      layer.bindPopup(
        `<b>District:</b> ${esc(feature.properties.map_district)}<br>` +
        `<b>Incidents:</b> ${esc(feature.properties.incidents)}<br>` +
        `<b>FRVs:</b> ${esc(feature.properties.frvs)}<br>` +
        `<b>Avg response:</b> ${esc(feature.properties.avg_resp)}`
      );
      layers.districts.push(layer);
    },
  }).addTo(map);

  L.geoJSON(payload.psGeojson, {
    style: psStyle,
    onEachFeature(feature, layer) {
      layer._kind     = "ps";
      layer._district = feature.properties.map_district;
      layer._psKey    = feature.properties.map_ps_key;
      layer.bindTooltip(feature.properties.map_ps);
      layer.bindPopup(
        `<b>Police Station:</b> ${esc(feature.properties.map_ps)}<br>` +
        `<b>District:</b> ${esc(feature.properties.map_district)}`
      );
      layers.psBoundaries.push(layer);
    },
  }).addTo(map);
}

// -- Point layers ------------------------------------------
function addPointLayers() {
  payload.psPoints.forEach(pt => {
    const marker = L.marker([pt.lat, pt.lon], { icon: makePsIcon(), zIndexOffset: 500 });
    marker._district = pt.district;
    marker._psKey    = pt.psKey;
    marker.bindTooltip("PS: " + pt.ps);
    marker.bindPopup(buildPsPopup(pt));
    marker.addTo(map);
    layers.psMarkers.push(marker);
  });
  // Add FRV markers to internal arrays (do not add them all to the map immediately)
  payload.currentFrvPoints.forEach(pt => {
    const marker = L.marker([pt.lat, pt.lon], { icon: makeFrvIcon(pt), zIndexOffset: 1000 });
    marker._district   = pt.district;
    marker._psKey      = pt.psKey;
    marker._deployment = "current";
    marker.bindTooltip("FRV " + pt.frvId);
    marker.bindPopup(buildFrvPopup(pt));
    marker.on('click', () => { if (window._showFrv) window._showFrv(pt.frvId, pt, "current"); });
    layers.currentFrvMarkers.push(marker);
    layers.frvMarkers.push(marker);
  });

  payload.optimizedFrvPoints.forEach(pt => {
    const marker = L.marker([pt.lat, pt.lon], { icon: makeFrvIcon(pt), zIndexOffset: 1000 });
    marker._district   = pt.district;
    marker._psKey      = pt.psKey;
    marker._deployment = "optimized";
    marker.bindTooltip("Optimized FRV " + pt.frvId);
    marker.bindPopup(buildFrvPopup(pt));
    marker.on('click', () => { if (window._showFrv) window._showFrv(pt.frvId, pt, "optimized"); });
    layers.optimizedFrvMarkers.push(marker);
  });

  buildTransferLayers(payload.transferLines);
  buildResponseHeatmap();
  // Initial render of visible markers
  updateVisibleFrvs();

  // Update on map move/zoom with debounce
  map.on('moveend zoomend', debounce(updateVisibleFrvs, 120));
}

// -- Icons -------------------------------------------------
function makePsIcon() {
  const color   = "#00aeff" ;

  return  L.divIcon({
    html: `<svg fill="${color}"> <use href="assets/police-station.svg"></use> </svg>`,
    className:  "",
    iconSize:  [28, 28],
    iconAnchor:[14, 14],
  });
}

function makeFrvIcon(pt) {
  const colorInfo = getFrvColorInfo(pt);

  return L.divIcon({
    html: `<svg fill="${colorInfo.color}"> <use href="assets/police-car.svg"></use> </svg>`,
    className:  "",
    iconSize:   [28, 28],
    iconAnchor: [14, 14],
  });
}

// -- Popup builders ----------------------------------------
function buildFrvPopup(pt) {
  const avg = getFrvAvgResponse(pt);
  const currentAvg = Number(pt.avg_response_before ?? pt.avgResponse ?? avg);
  const optimizedAvg = Number(pt.avg_response_after ?? avg);
  const maxResponse = Number(pt.maxResponse || 0);
  const colorInfo = getFrvColorInfo(pt);
  const color = colorInfo.color;
  const nearestLbl = pt.nearestDistance != null
    ? `${esc(pt.nearestPs)} (${Number(pt.nearestDistance).toFixed(2)} km)`
    : esc(pt.nearestPs || "N/A");
  const improvement = currentAvg && optimizedAvg
    ? ((currentAvg - optimizedAvg) / currentAvg) * 100
    : null;

  return `
    <div style="font-family:Segoe UI,Arial,sans-serif;min-width:285px;color:#17212b">
      <h3 style="margin:0 0 8px 0;font-size:17px;color:${color}">FRV ${esc(pt.frvId)}</h3>
      <table style="width:100%;font-size:13px;border-collapse:collapse">
        <tr><td style="padding:4px 0;color:#66717d"><b>District</b></td>
            <td style="text-align:right">${esc(pt.district)}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Police Station</b></td>
            <td style="text-align:right">${esc(pt.ps || "N/A")}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Avg RT</b></td>
            <td style="text-align:right">${formatMinutes(avg)}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Current Avg RT</b></td>
            <td style="text-align:right">${formatMinutes(currentAvg)}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Optimized Avg RT</b></td>
            <td style="text-align:right">${optimizedAvg ? formatMinutes(optimizedAvg) : "N/A"}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Improvement</b></td>
            <td style="text-align:right">${improvement != null ? improvement.toFixed(1) + "%" : "N/A"}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Transfer Source</b></td>
            <td style="text-align:right">${esc(pt.transfer_from_ps || "N/A")}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Transfer Destination</b></td>
            <td style="text-align:right">${esc(pt.transfer_to_ps || "N/A")}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Route length</b></td>
            <td style="text-align:right">${pt.patrolRouteLength ? pt.patrolRouteLength.toFixed(1) + " km" : "N/A"}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Assigned patrol zones</b></td>
            <td style="text-align:right">${pt.assignedPatrolZones || "N/A"}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Coverage</b></td>
            <td style="text-align:right">${pt.coverage ? pt.coverage + "%" : "N/A"}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Nearest PS</b></td>
            <td style="text-align:right">${nearestLbl}</td></tr>
      </table>
      <div style="margin-top:10px;padding:8px;border-radius:6px;background:${color};
                  color:#fff;text-align:center;font-weight:700">
        Avg response: ${formatMinutes(avg)}</div>
    </div>`;
}

function buildPsPopup(pt) {
  return `
    <div style="font-family:Segoe UI,Arial,sans-serif;min-width:260px;color:#17212b">
      <h3 style="margin:0 0 8px 0;font-size:17px;color:#1f6feb">Police Station</h3>
      <table style="width:100%;font-size:13px;border-collapse:collapse">
        <tr><td style="padding:4px 0;color:#66717d"><b>Name</b></td>
            <td style="text-align:right">${esc(pt.ps || "N/A")}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>District</b></td>
            <td style="text-align:right">${esc(pt.district || "N/A")}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>FRVs in PS</b></td>
            <td style="text-align:right">${Number(pt.frvCount || 0)}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Nearest FRV distance</b></td>
            <td style="text-align:right">${Number(pt.nearestDistance || 0).toFixed(2)} km</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Avg FRV distance</b></td>
            <td style="text-align:right">${Number(pt.avgDistance || 0).toFixed(2)} km</td></tr>
      </table>
    </div>`;
}

// -- Styles ------------------------------------------------
function districtStyle(feature) {
  const name   = feature.properties.map_district;
  const active = state.district && state.district === name;
  return {
    color:       active ? "#1f6feb" : "#334155",
    weight:      active ? 3 : 1.2,
    opacity:     active ? 1 : 0.42,
    fillColor:   "#60a5fa",
    fillOpacity: active ? 0.12 : 0.025,
  };
}

function psStyle(feature) {
  const key           = feature.properties.map_ps_key;
  const activeDistrict = state.district && feature.properties.map_district === state.district;
  const selected       = state.psKey && key === state.psKey;
  return {
    color:       selected ? "#e11d48" : activeDistrict ? "#f97316" : "#94a3b8",
    weight:      selected ? 4 : activeDistrict ? 2.1 : 1,
    opacity:     selected || activeDistrict ? 1 : 0.32,
    fillColor:   selected ? "#fb7185" : "#fdba74",
    fillOpacity: selected ? 0.20 : activeDistrict ? 0.10 : 0.02,
  };
}

// -- Filter logic ------------------------------------------
function layerVisible(layer, kind) {
  if (kind === "district")  return state.showDistricts && (!state.district || layer._district === state.district);
  if (kind === "ps")        return state.showPs        && (!state.district || layer._district === state.district);
  if (kind === "psMarker")  return state.showPsMarkers && (!state.district || layer._district === state.district);
  if (kind === "frv") {
    const isCurrent = layer._deployment === "current";
    const isOptimized = layer._deployment === "optimized";
    const visible = (isCurrent && state.showCurrentFrvs) || (isOptimized && state.showOptimizedFrvs);
    return visible
      && (!state.district || layer._district === state.district)
      && (!state.psKey    || layer._psKey    === state.psKey);
  }
  if (kind === "transfer") {
    return state.showTransfers && (!state.district || layer._district === state.district);
  }
  if (kind === "heatmap-current") {
    return state.showHeatmapCurrent && (!state.district || layer._district === state.district);
  }
  if (kind === "heatmap-optimized") {
    return state.showHeatmapOptimized && (!state.district || layer._district === state.district);
  }
  return true;
}

// Only render FRV markers within current viewport to improve performance
function updateVisibleFrvs() {
  if (!map) return;
  const bounds = map.getBounds();

  function handleList(list, group) {
    list.forEach(marker => {
      const should = layerVisible(marker, "frv") && bounds.contains(marker.getLatLng());
      const present = group.hasLayer(marker);
      if (should && !present) group.addLayer(marker);
      if (!should && present) group.removeLayer(marker);
    });
  }

  handleList(layers.currentFrvMarkers, layers.currentFrvGroup);
  handleList(layers.optimizedFrvMarkers, layers.optimizedFrvGroup);
}

// Debounce utility
function debounce(fn, wait = 120) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), wait); };
}

function setPresence(layer, show) {
  if (!map) return;
  const kind = layer._kind || layer._heatmapKind || (layer._deployment ? "frv" : null);
  // Route to appropriate group when available
  if (kind === "transfer" && layers.transferGroup) {
    const present = layers.transferGroup.hasLayer(layer);
    if (show && !present) layers.transferGroup.addLayer(layer);
    if (!show && present) layers.transferGroup.removeLayer(layer);
    return;
  }
  if ((layer._heatmapKind === "heatmap-current" || layer._heatmapKind === "heatmap-optimized") && layers.heatmapGroup) {
    const present = layers.heatmapGroup.hasLayer(layer);
    if (show && !present) layers.heatmapGroup.addLayer(layer);
    if (!show && present) layers.heatmapGroup.removeLayer(layer);
    return;
  }
  if (layer._deployment === "current" && layers.currentFrvGroup) {
    const present = layers.currentFrvGroup.hasLayer(layer);
    if (show && !present) layers.currentFrvGroup.addLayer(layer);
    if (!show && present) layers.currentFrvGroup.removeLayer(layer);
    return;
  }
  if (layer._deployment === "optimized" && layers.optimizedFrvGroup) {
    const present = layers.optimizedFrvGroup.hasLayer(layer);
    if (show && !present) layers.optimizedFrvGroup.addLayer(layer);
    if (!show && present) layers.optimizedFrvGroup.removeLayer(layer);
    return;
  }
  const has = map.hasLayer(layer);
  if (show && !has) map.addLayer(layer);
  if (!show && has) map.removeLayer(layer);
}

function applyFilters() {
  layers.districts.forEach(l => {
    if (l.setStyle) l.setStyle(districtStyle(l.feature));
    setPresence(l, layerVisible(l, "district"));
  });
  layers.psBoundaries.forEach(l => {
    if (l.setStyle) l.setStyle(psStyle(l.feature));
    setPresence(l, layerVisible(l, "ps"));
  });
  layers.psMarkers.forEach(l => setPresence(l, layerVisible(l, "psMarker")));
  // FRV markers are handled by viewport-aware renderer
  updateVisibleFrvs();
  layers.transferLines.forEach(l => setPresence(l, layerVisible(l, "transfer")));
  layers.heatmapMarkers.forEach(l => setPresence(l, layerVisible(l, l._heatmapKind)));
  updateNote();
}

// -- Fit helpers -------------------------------------------
function fitSelection() {
  if (state.psKey && payload.psBounds[state.psKey]) {
    map.fitBounds(payload.psBounds[state.psKey], { padding: [24, 24] });
    return;
  }
  if (state.district && payload.districtBounds[state.district]) {
    map.fitBounds(payload.districtBounds[state.district], { padding: [24, 24] });
    return;
  }
  if (payload.allBounds) {
    map.fitBounds(payload.allBounds, { padding: [18, 18] });
  } else {
    map.setView(payload.defaultCenter, payload.defaultZoom);
  }
}

// -- Dropdowns ---------------------------------------------
function populateDistricts() {
  const sel = document.getElementById("district-select");
  sel.innerHTML = '<option value="">View all</option>';
  Object.keys(payload.districtPsMap).sort().forEach(d => {
    const opt = document.createElement("option");
    opt.value       = d;
    opt.textContent = d;
    sel.appendChild(opt);
  });
}

function populatePoliceStations(district) {
  const sel = document.getElementById("ps-select");
  sel.innerHTML = "";
  if (!district) {
    sel.disabled     = true;
    sel.innerHTML    = '<option value="">Select district first</option>';
    return;
  }
  sel.disabled = false;
  const allOpt = document.createElement("option");
  allOpt.value       = "";
  allOpt.textContent = `All police stations in ${district}`;
  sel.appendChild(allOpt);
  (payload.districtPsMap[district] || []).forEach(item => {
    const opt = document.createElement("option");
    opt.value       = item.value;
    opt.textContent = item.label;
    sel.appendChild(opt);
  });
}

// -- Note text ---------------------------------------------
function updateNote() {
  const note     = document.getElementById("selection-note");
  const currentCount = layers.currentFrvMarkers.filter(l => map.hasLayer(l)).length;
  const optimizedCount = layers.optimizedFrvMarkers.filter(l => map.hasLayer(l)).length;
  const frvCount = currentCount + optimizedCount;
  const psCount  = layers.psMarkers.filter(l => map.hasLayer(l)).length;

  if (state.psKey) {
    const label = document.getElementById("ps-select").selectedOptions[0]?.textContent || "";
    note.textContent = `Showing ${frvCount} FRV marker(s) for ${label}.`;
  } else if (state.district) {
    note.textContent = `Showing ${psCount} police station(s) and ${frvCount} FRV(s) in ${state.district}.`;
  } else {
    note.textContent = `Showing ${currentCount} current FRVs${optimizedCount ? ` and ${optimizedCount} optimized FRVs` : ""}. Select a district to filter further.`;
  }
}

function setDeploymentMode() {
  const selected = document.querySelector('input[name="deployment-mode"]:checked');
  const mode = selected ? selected.value : "current";
  state.deploymentMode = mode;
  window._lbsDeploymentMode = mode;
  state.showCurrentFrvs = mode === "current";
  state.showOptimizedFrvs = mode === "optimized";
  const currentCheckbox = document.getElementById("toggle-current-frvs");
  const optimizedCheckbox = document.getElementById("toggle-optimized-frvs");
  if (currentCheckbox) currentCheckbox.checked = state.showCurrentFrvs;
  if (optimizedCheckbox) optimizedCheckbox.checked = state.showOptimizedFrvs;
  applyFilters();
  updateDeploymentNote();
}

function updateDeploymentNote() {
  const note = document.getElementById("deployment-note");
  if (!note) return;
  const optimizedExists = payload.optimizedFrvPoints && payload.optimizedFrvPoints.length;
  const identical = optimizedExists && deploymentsAreIdentical(payload);
  if (state.deploymentMode === "current") {
    note.textContent = "Showing current deployment layer with current FRV locations and response metrics.";
  } else if (state.deploymentMode === "optimized") {
    if (!optimizedExists || identical) {
      note.textContent = "Optimized deployment data is unavailable for this map.";
    } else {
      note.textContent = "Showing optimized deployment layer with transfer-aware FRV placement and expected response changes.";
    }
  } else {
    note.textContent = "Showing both current and optimized deployments side by side for comparison.";
  }
}

function deploymentsAreIdentical(payload) {
  const cur = (payload.currentFrvPoints || payload.frvPoints || []) || [];
  const opt = (payload.optimizedFrvPoints || []) || [];
  if (cur.length !== opt.length) return false;
  const mapCur = new Map(cur.map(p => [String(p.frvId || p.FRV_ID || `${p.lat},${p.lon}`), `${Number(p.lat).toFixed(6)},${Number(p.lon).toFixed(6)}`]));
  for (const o of opt) {
    const id = String(o.frvId || o.FRV_ID || `${o.lat},${o.lon}`);
    const coord = `${Number(o.lat).toFixed(6)},${Number(o.lon).toFixed(6)}`;
    if (!mapCur.has(id) || mapCur.get(id) !== coord) return false;
  }
  return true;
}

function buildResponseHeatmap() {
  layers.heatmapMarkers.forEach(l => map.removeLayer(l));
  layers.heatmapMarkers = [];

  payload.currentFrvPoints.forEach(pt => {
    if (pt.lat == null || pt.lon == null) return;
    const avg = getFrvAvgResponse(pt);
    const circle = L.circle([pt.lat, pt.lon], {
      radius: 6500,
      stroke: false,
      fillColor: responseHeatColor(avg),
      fillOpacity: 0.08,
    });
    circle._district = pt.district;
    circle._heatmapKind = "heatmap-current";
    layers.heatmapMarkers.push(circle);
  });

  payload.optimizedFrvPoints.forEach(pt => {
    if (pt.lat == null || pt.lon == null) return;
    const avg = getFrvAvgResponse(pt);
    const circle = L.circle([pt.lat, pt.lon], {
      radius: 6500,
      stroke: false,
      fillColor: responseHeatColor(avg),
      fillOpacity: 0.08,
    });
    circle._district = pt.district;
    circle._heatmapKind = "heatmap-optimized";
    layers.heatmapMarkers.push(circle);
  });
}

function responseHeatColor(avg) {
  if (avg <= 10) return "#16a34a";
  if (avg <= 20) return "#fde047";
  if (avg <= 30) return "#fb923c";
  return "#dc2626";
}

function makeArrowIcon(angle) {
  return L.divIcon({
    html: `<div style="transform:rotate(${angle}deg);color:#2563eb;font-size:18px;line-height:18px">➤</div>`,
    className: "",
    iconSize: [18, 18],
    iconAnchor: [9, 9],
  });
}

function bearing(from, to) {
  const dy = to[0] - from[0];
  const dx = to[1] - from[1];
  return Math.atan2(dy, dx) * 180 / Math.PI;
}

function average(values) {
  const valid = values.filter(v => Number.isFinite(v));
  return valid.length ? valid.reduce((sum, value) => sum + value, 0) / valid.length : 0;
}

function normalizeFrvResponseFields(points) {
  points.forEach(pt => {
    const canonical = Number(
      pt.avg_response_time_min
      ?? pt.avgResponse
      ?? pt.avg_response_after
      ?? pt.avg_response_before
      ?? pt.avg_response
      ?? pt.avgResponseMin
      ?? 0
    );
    pt.avg_response_time_min = Number.isFinite(canonical) ? canonical : 0;
  });
}

function normalizePsKey(district, ps) {
  return `${String(district || "").trim()}||${String(ps || "").trim()}`;
}

async function loadMapPayload() {
  const sources = [
    "map_data.json",
    "../data/outputs/map_data.json",
    "/data/outputs/map_data.json",
  ];
  for (const source of sources) {
    try {
      const res = await fetch(source);
      if (!res.ok) continue;
      const data = await res.json();
      if (data && data.frvPoints) return data;
    } catch (_e) {
      // try next source
    }
  }
  throw new Error("Could not locate valid map_data.json in web or data/outputs paths.");
}

function deriveOptimizedFrvPoints(payload) {
  const currentPoints = (payload.currentFrvPoints || []).map(pt => ({ ...pt }));
  const psLookup = payload.psLookup || {};
  const transferRows = payload.transferRows || [];
  const resimRows = payload.resimulationRows || [];

  const donorPools = {};
  const receivers = {};
  const movedFrvIds = new Set();
  const movedRecords = [];

  const currentByPs = {};
  currentPoints.forEach(pt => {
    const key = normalizePsKey(pt.district, pt.ps);
    currentByPs[key] = currentByPs[key] || [];
    currentByPs[key].push(pt);
  });

  transferRows.forEach((row, rowIndex) => {
    const donorKey = normalizePsKey(row.donor_district, row.donor_ps);
    const receiverKey = normalizePsKey(row.receiver_district, row.receiver_ps);
    if (!row.frvs_moved || Number(row.frvs_moved) <= 0) return;
    const count = Number(row.frvs_moved);
    donorPools[donorKey] = donorPools[donorKey] || [...(currentByPs[donorKey] || [])];
    receivers[receiverKey] = receivers[receiverKey] || { incoming: 0, donor: row.donor_ps, donorDistrict: row.donor_district };
    receivers[receiverKey].incoming += count;
    for (let index = 0; index < count; index += 1) {
      let source = donorPools[donorKey].shift();
      if (!source) {
        source = {
          frvId: `${row.donor_ps}-TRANSFER-${index + 1}`,
          district: row.donor_district,
          ps: row.donor_ps,
          psKey: donorKey,
          lat: psLookup[donorKey]?.lat,
          lon: psLookup[donorKey]?.lon,
          avgResponse: 0,
        };
      }
      movedFrvIds.add(source.frvId);
      const receiver = psLookup[receiverKey];
      const moved = {
        ...source,
        ps: row.receiver_ps,
        district: row.receiver_district,
        psKey: receiverKey,
        lat: receiver?.lat ?? source.lat,
        lon: receiver?.lon ?? source.lon,
        transfer_from_ps: row.donor_ps,
        transfer_to_ps: row.receiver_ps,
        transfer_from_district: row.donor_district,
        transfer_to_district: row.receiver_district,
        is_transferred: true,
      };
      movedRecords.push({
        frvId: moved.frvId,
        donor_ps: row.donor_ps,
        receiver_ps: row.receiver_ps,
        donor_district: row.donor_district,
        receiver_district: row.receiver_district,
        distance: receiver && psLookup[donorKey] ? haversineKm([psLookup[donorKey].lat, psLookup[donorKey].lon], [receiver.lat, receiver.lon]) : 0,
        rowIndex,
        row,
      });
    }
  });

  const optimized = currentPoints.filter(pt => !movedFrvIds.has(pt.frvId));
  optimized.push(...movedRecords.map(record => {
    const receiverKey = normalizePsKey(record.receiver_district, record.receiver_ps);
    const receiver = psLookup[receiverKey] || {};
    const original = currentPoints.find(pt => pt.frvId === record.frvId) || {};
    return {
      ...original,
      frvId: record.frvId,
      ps: record.receiver_ps,
      district: record.receiver_district,
      psKey: receiverKey,
      lat: receiver.lat ?? original.lat,
      lon: receiver.lon ?? original.lon,
      transfer_from_ps: record.donor_ps,
      transfer_to_ps: record.receiver_ps,
      transfer_from_district: record.donor_district,
      transfer_to_district: record.receiver_district,
      is_transferred: true,
    };
  }));

  const currentCounts = {};
  currentPoints.forEach(pt => {
    const key = normalizePsKey(pt.district, pt.ps);
    currentCounts[key] = (currentCounts[key] || 0) + 1;
  });

  const afterCounts = {};
  resimRows.forEach(row => {
    const key = normalizePsKey(row.district, row.ps);
    afterCounts[key] = Number(row.after_frvs) || 0;
  });

  Object.entries(afterCounts).forEach(([key, afterCount]) => {
    const currentCount = currentCounts[key] || 0;
    const incoming = (receivers[key]?.incoming || 0);
    const outgoing = (transferRows.filter(row => normalizePsKey(row.donor_district, row.donor_ps) === key).reduce((sum, row) => sum + (Number(row.frvs_moved) || 0), 0));
    const expected = currentCount + incoming - outgoing;
    const missing = afterCount - expected;
    if (missing > 0) {
      const receiver = psLookup[key];
      for (let i = 0; i < missing; i += 1) {
        optimized.push({
          frvId: `${receiver?.ps || "NEW"}-NEW-${String(i + 1).padStart(2, "0")}`,
          district: receiver?.district || "",
          ps: receiver?.ps || "",
          psKey: key,
          lat: receiver?.lat,
          lon: receiver?.lon,
          is_newly_added: true,
        });
      }
    }
  });

  payload.transferRecords = movedRecords;
  return optimized;
}

function buildTransferLines(transferRows, psLookup) {
  layers.transferLines.forEach(l => map.removeLayer(l));
  layers.transferLines = [];
  const records = [];

  transferRows.forEach(row => {
    const donorKey = normalizePsKey(row.donor_district, row.donor_ps);
    const receiverKey = normalizePsKey(row.receiver_district, row.receiver_ps);
    const donor = psLookup[donorKey];
    const receiver = psLookup[receiverKey];
    const count = Number(row.frvs_moved) || 0;
    if (!donor || !receiver || count <= 0) return;
    const distance = haversineKm([donor.lat, donor.lon], [receiver.lat, receiver.lon]);
    const color = buildTransferColor(row);
    const label = count === 1 ? "FRV" : `${count} FRVs`;
    const line = L.polyline([ [donor.lat, donor.lon], [receiver.lat, receiver.lon] ], {
      color,
      weight: 3,
      opacity: 0.8,
      dashArray: "10,6",
    });
    line._kind = "transfer";
    line._district = row.donor_district;
    line.bindPopup(
      `<div style="font-family:Segoe UI,Arial,sans-serif;min-width:240px;color:#17212b">` +
      `<h3 style="margin:0 0 8px 0;font-size:16px;color:${color}">Transfer</h3>` +
      `<table style="width:100%;font-size:13px;border-collapse:collapse">` +
      `<tr><td style="padding:4px 0;color:#66717d"><b>Transfer ID</b></td><td style="text-align:right">${esc(row.donor_ps)}→${esc(row.receiver_ps)}</td></tr>` +
      `<tr><td style="padding:4px 0;color:#66717d"><b>FRVs moved</b></td><td style="text-align:right"><b>${count}</b></td></tr>` +
      `<tr><td style="padding:4px 0;color:#66717d"><b>Donor PS</b></td><td style="text-align:right">${esc(row.donor_ps)}</td></tr>` +
      `<tr><td style="padding:4px 0;color:#66717d"><b>Receiver PS</b></td><td style="text-align:right">${esc(row.receiver_ps)}</td></tr>` +
      `<tr><td style="padding:4px 0;color:#66717d"><b>Transfer Distance</b></td><td style="text-align:right">${distance.toFixed(1)} km</td></tr>` +
      `</table></div>`
    );
    const arrow = L.marker([receiver.lat, receiver.lon], {
      icon: makeArrowIcon(bearing([donor.lat, donor.lon], [receiver.lat, receiver.lon])),
      interactive: false,
      zIndexOffset: 400,
    });
    arrow._kind = "transfer";
    arrow._district = row.donor_district;
    layers.transferLines.push(line, arrow);
    records.push({ line, arrow, row, count, distance });
  });

  return records;
}

function buildTransferColor(row) {
  if (row.donor_district === row.receiver_district) return "#16a34a";
  return "#f97316";
}
function getFrvAvgResponse(pt) {

    const value = Number(
        pt.avg_response_time_min ??
        pt.avgResponse ??
        pt.avg_response_after ??
        pt.avg_response_before ??
        pt.avg_response ??
        pt.avgResponseMin ??
        0
    );

    return Number.isFinite(value) ? value : 0;
}

function getFrvColorInfo(pt) {
  const avg = getFrvAvgResponse(pt);
  if (pt.is_newly_added) return { avg, color: "#8b5cf6", label: "PURPLE" };
  if (pt.transfer_from_ps || pt.transfer_to_ps) return { avg, color: "#2563eb", label: "BLUE" };
  if (avg <= 5) return { avg, color: "#16a34a", label: "GREEN" };
  if (avg <= 10) return { avg, color: "#f59e0b", label: "YELLOW" };
  return { avg, color: "#dc2626", label: "RED" };
}

function updateDynamicLegendItems() {
  const purpleItem = document.getElementById("legend-new-frv");
  if (!purpleItem) return;
  const hasNewFrvs = (payload.optimizedFrvPoints || []).some(pt => Object.prototype.hasOwnProperty.call(pt, "is_newly_added"));
  purpleItem.hidden = !hasNewFrvs;
}
function openPanel(panelId, btnId) {
  const panel     = document.getElementById(panelId);
  const shouldOpen = !panel.classList.contains("open");
  ["area-panel", "filter-panel", "patrol-panel", "deployment-panel", "legend-panel"].forEach(id =>
    document.getElementById(id).classList.toggle("open", id === panelId && shouldOpen)
  );
  ["area-btn", "filter-btn", "patrol-btn", "deployment-btn", "legend-btn"].forEach(id =>
    document.getElementById(id).classList.remove("active")
  );
  document.getElementById(btnId).classList.toggle("active", shouldOpen);
}

// -- Wire up UI --------------------------------------------
function wireControls() {
  const ui = document.getElementById("map-ui");
  if (L.DomEvent) {
    L.DomEvent.disableClickPropagation(ui);
    L.DomEvent.disableScrollPropagation(ui);
  }

  document.getElementById("area-btn").onclick        = () => openPanel("area-panel",   "area-btn");
  document.getElementById("filter-btn").onclick      = () => openPanel("filter-panel", "filter-btn");
  document.getElementById("patrol-btn").onclick      = () => openPanel("patrol-panel", "patrol-btn");
  document.getElementById("deployment-btn").onclick  = () => openPanel("deployment-panel", "deployment-btn");
  document.getElementById("legend-btn").onclick      = () => openPanel("legend-panel", "legend-btn");

  document.querySelectorAll('input[name="deployment-mode"]').forEach(input => {
    input.onchange = () => setDeploymentMode();
  });

  document.getElementById("district-select").onchange = e => {
    state.district = e.target.value;
    state.psKey    = "";
    populatePoliceStations(state.district);
    applyFilters();
    fitSelection();
  };

  document.getElementById("ps-select").onchange = e => {
    state.psKey = e.target.value;
    applyFilters();
    fitSelection();
  };

  [
    ["toggle-districts", "showDistricts"],
    ["toggle-ps",        "showPs"],
    ["toggle-ps-markers","showPsMarkers"],
  ].forEach(([id, key]) => {
    document.getElementById(id).onchange = e => {
      state[key] = e.target.checked;
      applyFilters();
    };
  });

  document.getElementById("toggle-current-frvs").onchange = e => {
    state.showCurrentFrvs = e.target.checked;
    state.showFrv = e.target.checked;
    applyFilters();
  };
  document.getElementById("toggle-optimized-frvs").onchange = e => {
    state.showOptimizedFrvs = e.target.checked;
    applyFilters();
  };
  document.getElementById("toggle-transfers").onchange = e => {
    state.showTransfers = e.target.checked;
    applyFilters();
  };
  document.getElementById("toggle-heatmap-current").onchange = e => {
    state.showHeatmapCurrent = e.target.checked;
    applyFilters();
  };
  document.getElementById("toggle-heatmap-optimized").onchange = e => {
    state.showHeatmapOptimized = e.target.checked;
    applyFilters();
  };
}

// -- Utilities ---------------------------------------------
function esc(v) {
  return String(v == null ? "" : v)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatMinutes(value) {
  const total   = Math.round(Number(value) * 60) || 0;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes ? `${minutes}m ${String(seconds).padStart(2, "0")}s` : `${seconds}s`;
}

function hideLoading() {
  document.getElementById("loading-overlay").classList.add("hidden");
}

function showError(msg) {
  const el = document.getElementById("loading-overlay");
  el.innerHTML = `<div style="color:#dc2626;font-size:15px;max-width:420px;text-align:center">
    <b>Error loading map data</b><br><br>${esc(msg)}<br><br>
    Make sure <code>map_data.json</code> is in the same folder as this page,
    and that you're serving it via a local HTTP server (not <code>file://</code>).
  </div>`;
}

// -- Start -------------------------------------------------
document.addEventListener("DOMContentLoaded", init);
