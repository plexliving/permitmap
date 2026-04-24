import React, { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";

const layerModules = import.meta.glob("../layers/*.json", { eager: true });
const LAYER_META = {
  duplex: { label: "Duplex", color: "#0f9b78", order: 2 },
  triplex: { label: "Triplex", color: "#d46a1f", order: 3 },
  fourplex: { label: "Fourplex", color: "#2563eb", order: 4 },
  fiveplex: { label: "Fiveplex", color: "#7c3aed", order: 5 },
  sixplex: { label: "Sixplex", color: "#be123c", order: 6 },
  sevenplex: { label: "Sevenplex", color: "#0f766e", order: 7 },
  eightplex: { label: "Eightplex", color: "#475569", order: 8 },
};

const LAYERS = Object.entries(layerModules)
  .map(([path, module]) => {
    const fileName = path.split("/").pop();
    const id = fileName.split("_")[0];
    const meta = LAYER_META[id] || { label: id, color: "#64748b", order: 99 };
    return {
      id,
      label: meta.label,
      fileName,
      color: meta.color,
      order: meta.order,
      data: module.default || module,
    };
  })
  .sort((a, b) => a.order - b.order || a.fileName.localeCompare(b.fileName));

const START_CENTER = [49.2463, -123.1162];
const START_ZOOM = 12;
const STATUS_SHADE_AMOUNTS = [0, 0.32, -0.28, 0.58, -0.48, 0.18, 0.74, -0.12];

function getLatLng(permit) {
  const point = permit.geo_point_2d;
  if (point && Number.isFinite(point.lat) && Number.isFinite(point.lon)) {
    return [point.lat, point.lon];
  }

  const coords = permit.geom?.geometry?.coordinates;
  if (
    Array.isArray(coords) &&
    coords.length >= 2 &&
    Number.isFinite(coords[0]) &&
    Number.isFinite(coords[1])
  ) {
    return [coords[1], coords[0]];
  }

  return null;
}

function makePin(color) {
  return L.divIcon({
    className: "permit-pin",
    html: `<span style="--pin-color: ${color}"></span>`,
    iconSize: [28, 36],
    iconAnchor: [14, 34],
    popupAnchor: [0, -30],
  });
}

function getPermitStatusLabel(permit) {
  return permit.permit_status || permit.status_group || "Unknown";
}

function getPermitStatusKey(permit) {
  return statusKeyFromLabel(getPermitStatusLabel(permit));
}

function statusKeyFromLabel(label) {
  return String(label).trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "unknown";
}

function colorForPermit(permit, layer, layerMode) {
  if (layerMode !== "status") return layer.color;
  return layer.statusColorByKey[getPermitStatusKey(permit)] || layer.color;
}

function shadeColor(hex, amount) {
  const rgb = hexToRgb(hex);
  if (!rgb) return hex;

  const target = amount >= 0 ? 255 : 0;
  const ratio = Math.abs(amount);
  const shaded = rgb.map((channel) => Math.round(channel + (target - channel) * ratio));
  return `rgb(${shaded[0]}, ${shaded[1]}, ${shaded[2]})`;
}

function hexToRgb(hex) {
  const value = hex.replace("#", "");
  if (!/^[0-9a-f]{6}$/i.test(value)) return null;
  return [
    parseInt(value.slice(0, 2), 16),
    parseInt(value.slice(2, 4), 16),
    parseInt(value.slice(4, 6), 16),
  ];
}

function formatMoney(value) {
  if (value === null || value === undefined || value === "") return "Not available";
  return new Intl.NumberFormat("en-CA", {
    style: "currency",
    currency: "CAD",
    maximumFractionDigits: 0,
  }).format(value);
}

function formatList(value) {
  if (Array.isArray(value)) return value.join(", ");
  return value || "Not available";
}

function popupHtml(permit, layerLabel) {
  const rows = [
    ["Permit ID", permit.permit_id],
    ["Layer", layerLabel],
    ["Status", permit.permit_status || permit.status_group],
    ["Address", permit.address],
    ["Created", permit.creation_date],
    ["Issued", permit.issued_date],
    ["Elapsed Days", permit.permitElapsedDays],
    ["Project Value", formatMoney(permit.projectValue)],
    ["Property Use", formatList(permit.PropertyUse)],
    ["Local Area", permit.geoLocalArea],
    ["Type of Work", permit.typeOfWork],
    ["Contact", permit.owner_or_contact_info],
    ["Description", permit.description],
  ];

  const tableRows = rows
    .map(([label, value]) => {
      const text = value === null || value === undefined || value === "" ? "Not available" : String(value);
      return `<tr><th>${escapeHtml(label)}</th><td>${escapeHtml(text)}</td></tr>`;
    })
    .join("");

  return `
    <section class="popup-content">
      <h2>${escapeHtml(permit.permit_id || "Permit")}</h2>
      <table>${tableRows}</table>
      ${
        permit.detail_url
          ? `<a href="${escapeHtml(permit.detail_url)}" target="_blank" rel="noreferrer">Open permit page</a>`
          : ""
      }
    </section>
  `;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function summarize(layers) {
  return layers.map((layer) => {
    const mapped = layer.data.filter((permit) => getLatLng(permit));
    const statusMap = new Map();
    mapped.forEach((permit) => {
      const label = getPermitStatusLabel(permit);
      const key = statusKeyFromLabel(label);
      const current = statusMap.get(key) || { key, label, count: 0 };
      current.count += 1;
      statusMap.set(key, current);
    });
    const statuses = [...statusMap.values()]
      .sort((a, b) => a.label.localeCompare(b.label))
      .map((status, index) => ({
        ...status,
        color: shadeColor(layer.color, STATUS_SHADE_AMOUNTS[index % STATUS_SHADE_AMOUNTS.length]),
      }));
    return {
      ...layer,
      mappedPermits: mapped,
      mappedCount: mapped.length,
      missingCount: layer.data.length - mapped.length,
      statuses,
      statusColorByKey: Object.fromEntries(statuses.map((status) => [status.key, status.color])),
    };
  });
}

function initialStatusFilters(layers) {
  return Object.fromEntries(
    layers.map((layer) => [
      layer.id,
      Object.fromEntries(layer.statuses.map((status) => [status.key, true])),
    ]),
  );
}

function initialLayerModes(layers) {
  return Object.fromEntries(layers.map((layer) => [layer.id, "layer"]));
}

function visiblePermitsForLayer(layer, statusFilters) {
  const layerStatusFilters = statusFilters[layer.id] || {};
  return layer.mappedPermits.filter((permit) => layerStatusFilters[getPermitStatusKey(permit)] !== false);
}

export default function App() {
  const mapNode = useRef(null);
  const mapRef = useRef(null);
  const layerRefs = useRef(new Map());
  const layerSummary = useMemo(() => summarize(LAYERS), []);
  const [enabled, setEnabled] = useState(() => Object.fromEntries(LAYERS.map((layer) => [layer.id, true])));
  const [expanded, setExpanded] = useState({});
  const [layerModes, setLayerModes] = useState(() => initialLayerModes(layerSummary));
  const [statusFilters, setStatusFilters] = useState(() => initialStatusFilters(layerSummary));

  useEffect(() => {
    if (!mapNode.current || mapRef.current) return;

    const map = L.map(mapNode.current, {
      center: START_CENTER,
      zoom: START_ZOOM,
      zoomControl: false,
    });
    L.control.zoom({ position: "bottomright" }).addTo(map);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors",
    }).addTo(map);

    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    layerRefs.current.forEach((group) => group.removeFrom(map));
    layerRefs.current.clear();

    const bounds = [];
    layerSummary.forEach((layer) => {
      if (!enabled[layer.id]) return;

      const group = L.layerGroup();
      visiblePermitsForLayer(layer, statusFilters).forEach((permit) => {
        const latLng = getLatLng(permit);

        L.marker(latLng, { icon: makePin(colorForPermit(permit, layer, layerModes[layer.id])) })
          .bindPopup(popupHtml(permit, layer.label), { maxWidth: 420 })
          .addTo(group);
        bounds.push(latLng);
      });

      group.addTo(map);
      layerRefs.current.set(layer.id, group);
    });

    if (bounds.length) {
      map.fitBounds(bounds, { padding: [34, 34], maxZoom: 14 });
    }
  }, [enabled, layerModes, layerSummary, statusFilters]);

  const visiblePins = layerSummary.reduce(
    (sum, layer) => sum + (enabled[layer.id] ? visiblePermitsForLayer(layer, statusFilters).length : 0),
    0,
  );

  return (
    <main className="app-shell">
      <aside className="sidebar" aria-label="Permit layers">
        <div className="brand-block">
          <p className="eyebrow">Permitmap Preview</p>
          <h1>Vancouver Multiplex Permits</h1>
        </div>

        <section className="panel">
          <div className="panel-heading">
            <h2>Layers</h2>
            <span>{visiblePins} pins</span>
          </div>

          <div className="layer-list">
            {layerSummary.map((layer) => {
              const visibleLayerPermits = visiblePermitsForLayer(layer, statusFilters);
              const isExpanded = Boolean(expanded[layer.id]);
              return (
                <article className="layer-item" key={layer.id}>
                  <div className="layer-toggle">
                    <button
                      type="button"
                      className="expand-button"
                      aria-label={`${isExpanded ? "Collapse" : "Expand"} ${layer.label}`}
                      aria-expanded={isExpanded}
                      onClick={() =>
                        setExpanded((current) => ({
                          ...current,
                          [layer.id]: !current[layer.id],
                        }))
                      }
                    >
                      <span className={isExpanded ? "triangle expanded" : "triangle"} />
                    </button>
                    <input
                      type="checkbox"
                      aria-label={`Toggle ${layer.label}`}
                      checked={enabled[layer.id]}
                      onChange={(event) =>
                        setEnabled((current) => ({
                          ...current,
                          [layer.id]: event.target.checked,
                        }))
                      }
                    />
                    <span className="swatch" style={{ backgroundColor: layer.color }} />
                    <span className="layer-name">{layer.label}</span>
                    <span className="layer-count">{visibleLayerPermits.length}/{visibleLayerPermits.length}</span>
                  </div>

                  {isExpanded ? (
                    <div className="layer-details">
                      <fieldset className="layer-mode">
                        <legend>Colour mode</legend>
                        <label>
                          <input
                            type="radio"
                            name={`${layer.id}-mode`}
                            value="layer"
                            checked={layerModes[layer.id] !== "status"}
                            onChange={() =>
                              setLayerModes((current) => ({
                                ...current,
                                [layer.id]: "layer",
                              }))
                            }
                          />
                          <span>Do not separate</span>
                        </label>
                        <label>
                          <input
                            type="radio"
                            name={`${layer.id}-mode`}
                            value="status"
                            checked={layerModes[layer.id] === "status"}
                            onChange={() =>
                              setLayerModes((current) => ({
                                ...current,
                                [layer.id]: "status",
                              }))
                            }
                          />
                          <span>Separate by permit status</span>
                        </label>
                      </fieldset>

                      <div className="status-filter-list" aria-label={`${layer.label} permit statuses`}>
                        {layer.statuses.length ? (
                          layer.statuses.map((status) => (
                            <label className="status-filter" key={status.key}>
                              <input
                                type="checkbox"
                                checked={statusFilters[layer.id]?.[status.key] !== false}
                                onChange={(event) =>
                                  setStatusFilters((current) => ({
                                    ...current,
                                    [layer.id]: {
                                      ...(current[layer.id] || {}),
                                      [status.key]: event.target.checked,
                                    },
                                  }))
                                }
                              />
                              <span className="status-swatch" style={{ backgroundColor: status.color }} />
                              <span>{status.label}</span>
                              <span className="status-count">{status.count}</span>
                            </label>
                          ))
                        ) : (
                          <p className="empty-layer">No mappable permits in this layer.</p>
                        )}
                      </div>
                    </div>
                  ) : null}
                </article>
              );
            })}
          </div>
        </section>

        <section className="panel">
          <h2>Layer Files</h2>
          <ul className="file-list">
            {LAYERS.map((layer) => (
              <li key={layer.id}>
                <span className="dot" style={{ backgroundColor: layer.color }} />
                <span>{layer.fileName}</span>
              </li>
            ))}
          </ul>
        </section>
      </aside>

      <section className="map-area" aria-label="Permit map">
        <div ref={mapNode} className="map" />
      </section>
    </main>
  );
}
