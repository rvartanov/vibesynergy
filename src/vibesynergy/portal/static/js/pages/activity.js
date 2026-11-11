// One activity: summary, route map (MapLibre), stream charts as aligned panels sharing the
// elapsed-time axis (hovering a chart moves a marker along the route), laps, time in zones
// and the copy from each source.
import * as maplibregl from "../../vendor/maplibre/maplibre-gl.mjs";
import { annotationPreview } from "../annotation_preview.js";
import { get, post } from "../api.js";
import { bindEffort, effortHtml } from "../effort.js";
import { alpha, mount, onLeave, series, token, tooltipBase } from "../charts.js";
import {
  clock,
  esc,
  hoursMinutes,
  isNum,
  longDate,
  number,
  sourceName,
  sportName,
  tagName,
  timeOfDay,
} from "../format.js";
import { N, t } from "../i18n.js";
import { bindNoteForm, noteFormHtml } from "../quicklog.js";
import { distanceText, sportFamily, speedChannel, speedLabel, speedText } from "../sport_units.js";
import { withSportIcon, tagChip } from "../symbols.js";
import {
  zoneAt,
  zoneBadge,
  zoneLabel,
  zoneLegend,
  zoneMissing,
  zoneRows,
  zoneScale,
  zoneTable,
  zoneTitle,
  zoneToken,
} from "../zone_presentation.js";

// The stream channel key for pace/speed ("speed") differs from the zone kind it's recorded
// under ("pace"); every other channel key already matches its zone kind name.
function zoneKind(channelKey) {
  return channelKey === "speed" ? "pace" : channelKey;
}

// Stream channels shown as panels, in this order when present.
function channels(sport) {
  const family = sportFamily(sport);
  return [
    ["hr", t("Heart rate"), "bpm", (v) => Math.round(v)],
    speedChannel(sport),
    ["power", t("Power"), "W", (v) => Math.round(v)],
    ["cadence", t("Cadence"), family === "run" ? "spm" : "rpm", (v) => Math.round(v)],
    ["alt", t("Elevation"), "m", (v) => v],
  ].filter(Boolean);
}

// VibeSynergy's own training load next to intervals.icu's; provenance, so collapsed.
const LOAD_METHODS = { power: N("from power"), hr_stream: N("from heart rate"), hr: N("from average heart rate"), pace: N("from pace") };
function ownLoadHtml(load) {
  if (!load) return "";
  return `<details class="card collapsible"><summary>${esc(t("Training load: {n}", { n: number(load.intervals) }))}</summary>
    <p class="sub">${esc(t("intervals.icu {theirs} · VibeSynergy {mine} ({method}). intervals.icu counts for your fitness; VibeSynergy's own number is being checked against it.", { theirs: number(load.intervals), mine: number(load.vibesynergy), method: t(LOAD_METHODS[load.method] || load.method) }))}</p></details>`;
}

// A multisport holder lists its parts; a part links back to its holder.
function multisportHtml(ms) {
  if (!ms) return "";
  const link = (x) => `<a href="#/activity/${encodeURIComponent(x.group_id)}">${esc(x.name || sportName(x.sport))}</a>`;
  if (ms.holder) return `<p class="sub">${esc(t("Part of the multisport"))} ${link(ms.holder)}</p>`;
  const rows = (ms.parts || []).map((p) => `<li>${withSportIcon(p.sport, link(p))}${isNum(p.moving_s) ? ` <span class="sub">${esc(hoursMinutes(p.moving_s))}</span>` : ""}</li>`).join("");
  return rows ? `<section class="card"><h2>${esc(t("Parts"))}</h2><ul class="plain">${rows}</ul></section>` : "";
}

export async function render(view, { args, meta, stale }) {
  const id = args[0];
  if (!id) {
    location.hash = "#/activities";
    return;
  }
  const reply = await get("activity", { id, max_points: 1500 });
  if (stale()) return;
  const { activity: a, sources, laps, zones, stream_data: streams, notes, effort } = reply.data;
  const tz = reply.meta.timezone;
  const title = a.name || sportName(a.sport);
  document.title = `${title} · VibeSynergy`;
  const cols = streams?.columns || {};
  const hasRoute = (cols.lat || []).some(isNum) && (cols.lon || []).some(isNum);
  const panels = channels(a.sport).filter(([key]) => (cols[key] || []).some((v) => isNum(v) && v !== 0));

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1>${esc(title)}</h1>
        <div class="sub">${withSportIcon(a.sport, esc(sportName(a.sport)))} · ${esc(longDate(a.local_date))} · ${esc(timeOfDay(a.start_utc, tz))}
          · <a href="#/dashboard/${esc(a.local_date)}">${t("Open this day")}</a></div>
      </div>
      <a class="btn" href="#/activities">${t("All activities")}</a>
    </div>
    <section class="card facts-card">${factsHtml(a)}</section>
    ${multisportHtml(reply.data.multisport)}
    ${effortCardHtml(effort, !!meta.data.share)}
    <section class="card activity-notes" data-activity-notes${notes.length ? "" : " hidden"}>${notesHtml(notes)}</section>
    ${hasRoute ? `<section class="card map-card"><div class="map" role="region" aria-label="${esc(t("Route map"))}"></div></section>` : ""}
    ${panels.length ? `<section class="card"><h2>${t("Over time")}</h2><div class="streams" role="img" aria-label="${esc(t("{channels} over the activity", { channels: panels.map((p) => p[1]).join(", ") }))}"></div>${streamZoneLegend(zones, panels, a.sport)}</section>` : ""}
    ${laps.length > 1 ? `<section class="card">${lapsHtml(laps, a.sport, zones)}</section>` : ""}
    <section class="grid two">
      ${zones.length ? `<div class="card">${zonesHtml(zones, a.sport)}</div>` : ""}
      <div class="card">${sourcesHtml(sources, a)}</div>
    </section>
    <p class="sub" data-activity-weather></p>
    ${ownLoadHtml(reply.data.own_load)}
    ${meta.data.share ? "" : `<details class="card collapsible activity-settings" data-activity-settings>
      <summary>${t("Settings for this activity")}</summary>
      ${gearPickerHtml(reply.data.gear)}
      <section aria-labelledby="activity-purpose-title"><h3 id="activity-purpose-title">${t("Session purpose")}</h3><label>${t("Purpose")}
      <select class="btn" data-purpose-choice>
        <option value="auto">${t("Use automatic rules")}</option>
        <option value="training">${t("Training")}</option>
        <option value="recovery">${t("Recovery")}</option>
        <option value="leisure">${t("Leisure")}</option>
        <option value="coached_by_me">${t("Coached by me")}</option>
        <option value="race">${t("Race")}</option>
      </select></label><p class="sub" data-purpose-status></p></section>
      <div data-annotation-preview></div>
    </details>`}`;

  const effortHost = view.querySelector("[data-effort-host]");
  if (effortHost && !meta.data.share) bindEffort(effortHost, id);
  if (!meta.data.share) bindActivityNote(view, id, notes, stale);
  const purpose = view.querySelector("[data-purpose-choice]");
  if (purpose) {
    purpose.value = a.purpose || "auto";
    const purposeStatus = view.querySelector("[data-purpose-status]");
    purposeStatus.textContent = a.purpose
      ? t("Saved purpose: {purpose}.", { purpose: t(purposeLabel(a.purpose)) })
      : t("No purpose tag yet; threshold updates skip this session until you tag it.");
    purpose.addEventListener("change", async () => {
      try {
        const value = purpose.value === "auto" ? null : purpose.value;
        await post("activity-purpose", { id, purpose: value });
        purposeStatus.textContent = value
          ? t("Saved purpose: {purpose}.", { purpose: t(purposeLabel(value)) })
          : t("Back to automatic rules.");
      } catch (error) { purposeStatus.textContent = t("Not saved: {error}", { error: error.message }); }
    });
  }
  bindGearPicker(view, id);
  // The weather of an outdoor session: Garmin's, else Open-Meteo at its start and
  // hour. Fetched after the page shows, so a slow weather service never holds it up.
  get("activity-weather", { id }).then((r) => {
    const el = view.querySelector("[data-activity-weather]");
    if (el && !stale() && r.data?.line) el.textContent = `${t("Weather")}: ${r.data.line}`;
  }, () => {});
  if (!meta.data.share) annotationPreview(view.querySelector("[data-annotation-preview]"), id, stale,
    sources.map((source) => source.id.split(":")[0]).filter((source) => ["strava", "garmin"].includes(source)));
  let marker = null;
  if (hasRoute) marker = routeMap(view.querySelector(".map"), cols, meta.data.tiles);
  if (panels.length) streamCharts(view.querySelector(".streams"), cols, panels, marker, zones);
}

// Gear: yours wins over the source's; it counts in the gear totals at once.
const GEAR_HOW = { own: N("Set by you."), source: N("From Strava."), default: N("The default for this sport.") };

function gearPickerHtml(gear) {
  if (!gear || !gear.options?.length) return "";
  const option = (value, label) => `<option value="${esc(value)}"${value === (gear.how === "own" ? gear.ref || "none" : "auto") ? " selected" : ""}>${esc(label)}</option>`;
  const current = gear.options.find((g) => g.ref === gear.ref);
  const auto = gear.how && gear.how !== "own" && current ? t("Automatic: {name}", { name: current.name }) : t("Automatic");
  return `<section aria-labelledby="activity-gear-title"><h3 id="activity-gear-title">${t("Gear")}</h3><label>${t("Gear used")}
    <select class="btn" data-gear-choice>
      ${option("auto", auto)}
      ${gear.options.map((g) => option(g.ref, g.name || g.ref)).join("")}
      ${option("none", t("No gear"))}
    </select></label><p class="sub" data-gear-status>${gear.how ? esc(t(GEAR_HOW[gear.how])) : ""}</p></section>`;
}

function bindGearPicker(view, id) {
  const select = view.querySelector("[data-gear-choice]");
  if (!select) return;
  const status = view.querySelector("[data-gear-status]");
  select.addEventListener("change", async () => {
    try {
      await post("activity-gear", { id, ref: select.value === "auto" ? null : select.value });
      status.textContent = select.value === "auto" ? t("Back to automatic.") : t("Saved. It counts in the gear totals.");
    } catch (error) { status.textContent = t("Not saved: {error}", { error: error.message }); }
  });
}

// RPE and Feel: shown with their source; filled in here when no platform has them.
function effortCardHtml(effort, shared) {
  const body = effortHtml(effort, "activity", { readOnly: shared });
  if (!body && shared) return "";
  return `<section class="card" aria-labelledby="activity-effort-title"><h2 id="activity-effort-title">${t("How it went")}</h2>
    ${body ? `<div data-effort-host>${body}</div>` : ""}
    ${shared ? "" : `<div class="ql-actions"><button class="btn" type="button" data-note-open aria-expanded="false">+ ${t("Note")}</button></div><div data-note-slot></div>`}</section>`;
}

// A note about this activity: it lands in the diary, linked to this workout, and
// ticking pain also logs it in the pain log.
function bindActivityNote(view, id, notes, stale) {
  const button = view.querySelector("[data-note-open]");
  const slot = view.querySelector("[data-note-slot]");
  if (!button || !slot) return;
  const shown = [...notes];
  button.addEventListener("click", async () => {
    if (slot.querySelector("form")) {
      slot.innerHTML = "";
      button.setAttribute("aria-expanded", "false");
      return;
    }
    button.disabled = true;
    try {
      const reply = await get("diary", { period: "today" });
      if (stale() || !slot.isConnected) return;
      slot.innerHTML = noteFormHtml(reply.data.choices, { activityId: id, key: "activity", hidden: false });
      button.setAttribute("aria-expanded", "true");
      const form = slot.querySelector("form");
      bindNoteForm(form, {
        onSaved: (message, note) => {
          if (!slot.isConnected) return;
          if (note?.id) shown.push(note);
          const list = view.querySelector("[data-activity-notes]");
          if (list) {
            list.innerHTML = notesHtml(shown);
            list.hidden = !shown.length;
          }
          slot.innerHTML = `<p class="sub ok-text" role="status">${esc(message)}</p>`;
          button.setAttribute("aria-expanded", "false");
          button.focus();
        },
      });
      form.querySelector("textarea")?.focus();
    } catch (error) {
      slot.innerHTML = `<p class="sub error-text" role="status">${esc(error.message)}</p>`;
    } finally {
      button.disabled = false;
    }
  });
}

function factsHtml(a) {
  const family = sportFamily(a.sport);
  const units = a.sport_units || {};
  const facts = [
    [t("Distance"), a.distance_m ? distanceText(a.sport, a.distance_m) : null],
    [t("Moving time"), a.moving_s ? hoursMinutes(a.moving_s) : null],
    [t("Elapsed time"), a.elapsed_s ? clock(a.elapsed_s) : null],
    [speedLabel(a.sport, true), a.distance_m && isNum(a.avg_speed) ? speedText(a.sport, a.avg_speed) : null],
    [t("Average HR"), isNum(a.avg_hr) ? `${Math.round(a.avg_hr)} bpm` : null],
    [t("Max HR"), isNum(a.max_hr) ? `${Math.round(a.max_hr)} bpm` : null],
    [t("Average power"), isNum(a.avg_power) ? `${Math.round(a.avg_power)} W` : null],
    ["W/kg", units.power_per_kg],
    [t("Pool length"), family === "pool_swim" && units.pool_length_m != null ? `${number(Number(units.pool_length_m))} m` : null],
    [t("Lengths"), family === "pool_swim" ? units.lengths : null],
    ["SWOLF", family === "pool_swim" ? units.swolf : null],
    [t("Strokes"), family === "pool_swim" ? units.strokes : null],
    [t("Stroke rate"), family === "pool_swim" ? units.stroke_rate : null],
    [t("Sets"), family === "time" ? units.sets : null],
    [t("Reps"), family === "time" ? units.reps : null],
    [t("Normalized power"), isNum(a.norm_power) ? `${Math.round(a.norm_power)} W` : null],
    [t("Climb"), isNum(a.ascent_m) && a.ascent_m > 0 ? `${number(a.ascent_m)} m` : null],
    [t("Calories"), isNum(a.calories) ? `${number(a.calories)} kcal` : null],
    [t("Training load"), isNum(a.training_load) ? number(a.training_load) : null],
  ].filter(([k, v]) => k && v);
  return `<div class="facts">${facts.map(([k, v]) => `<div>${esc(k)}<b>${esc(v)}</b></div>`).join("")}</div>`;
}

function notesHtml(notes) {
  return `<h2>${t("Diary")}</h2><ul class="activity-notes-list">${notes
    .map((n) => {
      const tags = n.tags.map((tag) => tagChip(tag, tagName(tag))).join(" ");
      return `<li>${tags} ${esc(n.text)}<div class="sub">${esc(sourceName(n.source))} · <a href="#/dashboard?section=diary">${t("Diary")}</a></div></li>`;
    })
    .join("")}</ul>`;
}

// Map ------------------------------------------------------------------------------------

function routeMap(el, cols, tiles) {
  const coords = [];
  cols.lat.forEach((lat, i) => {
    const lon = cols.lon[i];
    if (isNum(lat) && isNum(lon) && (lat !== 0 || lon !== 0)) coords.push([lon, lat]);
  });
  const lons = coords.map((c) => c[0]);
  const lats = coords.map((c) => c[1]);
  const bounds = [
    [Math.min(...lons), Math.min(...lats)],
    [Math.max(...lons), Math.max(...lats)],
  ];
  const dark = getComputedStyle(document.documentElement).colorScheme === "dark";
  const map = new maplibregl.Map({
    container: el,
    style: {
      version: 8,
      sources: {
        osm: {
          type: "raster",
          // Plain concatenation: new URL() would encode the {z}/{x}/{y} placeholders.
          // MapLibre needs an absolute address; the local cache's is relative to the page.
          tiles: [/^https?:/.test(tiles) ? tiles : `${new URL(".", document.baseURI).href}${tiles}`],
          tileSize: 256,
          maxzoom: 19,
          attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
        },
      },
      layers: [
        {
          id: "osm",
          type: "raster",
          source: "osm",
          paint: dark ? { "raster-brightness-max": 0.55, "raster-saturation": -0.6 } : { "raster-saturation": -0.3 },
        },
      ],
    },
    bounds,
    fitBoundsOptions: { padding: 32 },
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.scrollZoom.disable(); // the page scrolls; zoom with the buttons, pinch or double-click
  onLeave(() => map.remove());

  const line = series(1);
  const surface = token("surface");
  map.on("load", () => {
    map.addSource("route", { type: "geojson", data: { type: "Feature", geometry: { type: "LineString", coordinates: coords } } });
    map.addLayer({ id: "route-casing", type: "line", source: "route", paint: { "line-color": surface, "line-width": 6 }, layout: { "line-cap": "round", "line-join": "round" } });
    map.addLayer({ id: "route", type: "line", source: "route", paint: { "line-color": line, "line-width": 3 }, layout: { "line-cap": "round", "line-join": "round" } });
    const ends = [
      { type: "Feature", properties: { kind: "start" }, geometry: { type: "Point", coordinates: coords[0] } },
      { type: "Feature", properties: { kind: "end" }, geometry: { type: "Point", coordinates: coords[coords.length - 1] } },
    ];
    map.addSource("ends", { type: "geojson", data: { type: "FeatureCollection", features: ends } });
    map.addLayer({
      id: "ends",
      type: "circle",
      source: "ends",
      paint: {
        "circle-radius": 6,
        "circle-color": ["match", ["get", "kind"], "start", token("msg-success"), token("msg-danger")],
        "circle-stroke-color": surface,
        "circle-stroke-width": 2,
      },
    });
    map.addSource("hover", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "hover",
      type: "circle",
      source: "hover",
      paint: { "circle-radius": 7, "circle-color": line, "circle-stroke-color": surface, "circle-stroke-width": 2 },
    });
  });

  // Move the hover dot to the sample at index i (or hide it with null).
  return (i) => {
    const source = map.getSource("hover");
    if (!source) return;
    const lat = i === null ? null : cols.lat[i];
    const lon = i === null ? null : cols.lon[i];
    source.setData({
      type: "FeatureCollection",
      features: isNum(lat) && isNum(lon) ? [{ type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] } }] : [],
    });
  };
}

// Stream charts --------------------------------------------------------------------------

// Keep a point when it is in the zone, and one following point so the stroke meets the
// next zone. Null samples stay gaps.
function maskZone(points, match) {
  const out = [];
  for (let i = 0; i < points.length; i++) {
    const point = points[i];
    const on = isNum(point[1]) && isNum(point[2]) && match(point);
    const prev = i > 0 && isNum(points[i - 1][1]) && isNum(points[i - 1][2]) && match(points[i - 1]);
    out.push(on || prev ? point : [point[0], null, point[2]]);
  }
  return out;
}

function streamCharts(el, cols, panels, marker, zones) {
  const scales = panels.map(([kind]) => zoneScale(zones, zoneKind(kind)));
  const x = cols.elapsed_s;
  const height = 92;
  const gap = 34;
  const grids = panels.map((_, i) => ({ left: 48, right: 16, top: 20 + i * (height + gap), height }));
  el.style.height = `${20 + panels.length * (height + gap) + 4}px`;
  const muted = token("muted");
  const gridColor = token("grid");
  const color = series(1);
  const maxX = x[x.length - 1] || 1;
  const step = maxX > 7200 ? 1800 : maxX > 2400 ? 600 : maxX > 900 ? 300 : 60;

  const chart = mount(el, {
    // This ECharts build does not paint a line from visualMap pieces, so each zone is its
    // own line in the shared zone colour. Classification uses the recorded value (bpm, W
    // or m/s), not the plotted one: pace and cycling speed convert m/s for display.
    grid: grids,
    xAxis: panels.map((_, i) => ({
      type: "value",
      gridIndex: i,
      min: 0,
      max: maxX,
      interval: step,
      axisLabel: { show: i === panels.length - 1, color: muted, formatter: (v) => clock(v) },
      axisLine: { lineStyle: { color: token("axis") } },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: gridColor } },
    })),
    yAxis: panels.map(([key, , , , fmt], i) => ({
      type: "value",
      gridIndex: i,
      scale: true,
      inverse: key === "speed" && panels[i][2].startsWith("/"), // pace: faster (lower) is up
      splitNumber: 2,
      axisLabel: { color: muted, fontSize: 11, formatter: fmt || ((v) => number(v)) },
      splitLine: { lineStyle: { color: gridColor } },
    })),
    graphic: panels.map(([, title, unit], i) => ({
      type: "text",
      left: 48,
      top: grids[i].top - 17,
      style: { text: `${title} (${unit})`, fill: token("ink-2"), fontSize: 12, fontWeight: 600 },
    })),
    series: panels.flatMap(([key, title, , transform], i) => {
      const points = x.map((elapsed, j) => {
        const raw = cols[key][j];
        return [elapsed, isNum(raw) ? transform(raw) : null, isNum(raw) ? raw : null];
      });
      const base = {
        type: "line",
        name: title,
        xAxisIndex: i,
        yAxisIndex: i,
        showSymbol: false,
        connectNulls: false,
      };
      if (!scales[i].length) {
        return [{
          ...base,
          id: `stream-${i}`,
          sampling: "lttb",
          data: points,
          lineStyle: { width: 2, color, join: "round" },
          itemStyle: { color },
          areaStyle: key === "alt" ? { color: alpha(color, 0.1) } : undefined,
        }];
      }
      const zoneNumbers = [...new Set(scales[i].map((z) => z.zone))];
      const colored = zoneNumbers.map((zone) => ({
        ...base,
        id: `stream-${i}-z${zone}`,
        data: maskZone(points, (point) => zoneAt(scales[i], point[2]) === zone),
        lineStyle: { width: 2, color: token(zoneToken(zone)), join: "round" },
        itemStyle: { color: token(zoneToken(zone)) },
      }));
      const outside = points.some((point) => isNum(point[2]) && zoneAt(scales[i], point[2]) === null);
      if (outside) {
        colored.push({
          ...base,
          id: `stream-${i}-other`,
          data: maskZone(points, (point) => isNum(point[2]) && zoneAt(scales[i], point[2]) === null),
          lineStyle: { width: 2, color: token("ink-2"), join: "round" },
        });
      }
      return colored;
    }),
    axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: token("axis") } },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      axisPointer: { type: "line" },
      formatter: (items) => {
        if (!items.length) return "";
        const lines = [`<span style="color:${token("ink-2")}">${clock(items[0].axisValue)}</span>`];
        const seen = new Set();
        for (const item of items) {
          const match = /^stream-(\d+)(?:-z(\d+)|-other)?$/.exec(item.seriesId || "");
          if (!match) continue;
          const index = Number(match[1]);
          const panel = panels[index];
          const v = item.value?.[1];
          if (!panel || !isNum(v) || seen.has(index)) continue;
          const zone = zoneAt(scales[index], item.value[2]);
          // The point that joins two strokes belongs to the previous zone's line.
          if (match[2] !== undefined && Number(match[2]) !== zone) continue;
          if (item.seriesId.endsWith("-other") && zone !== null) continue;
          seen.add(index);
          const text = panel[4] ? panel[4](v) : number(v, panel[0] === "speed" ? 1 : 0);
          const suffix = zone === null ? "" : ` · ${esc(zoneLabel(zone))}`;
          lines.push(`${esc(panel[1])}: <b>${esc(text)} ${esc(panel[2])}</b>${suffix}`);
        }
        return lines.join("<br>");
      },
    },
  });
  if (marker) {
    chart.on("updateAxisPointer", (event) => {
      const value = event.axesInfo?.[0]?.value;
      if (!isNum(value)) return marker(null);
      let lo = 0;
      let hi = x.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (x[mid] < value) lo = mid + 1;
        else hi = mid;
      }
      marker(lo);
    });
    chart.getZr().on("globalout", () => marker(null));
  }
}

// Tables ---------------------------------------------------------------------------------

function lapsHtml(laps, sport, zones) {
  const hrScale = zoneScale(zones, "hr");
  const powerScale = zoneScale(zones, "power");
  const paceScale = zoneScale(zones, "pace");
  const lapOffset = Math.min(...laps.map((lap) => lap.lap_index)) === 0 ? 1 : 0;
  const family = sportFamily(sport);
  const paced = Boolean(speedLabel(sport));
  const swim = family === "pool_swim" || family === "open_water_swim";
  const extraColumns = swim ? [
    ["lengths", t("Lengths")], ["swolf", "SWOLF"], ["strokes", t("Strokes")],
    ["stroke_rate", t("Stroke rate")], ["rest_s", t("Rest")],
  ] : family === "time" ? [["sets", t("Sets")], ["reps", t("Reps")]] : [];
  const shown = extraColumns.filter(([key]) => laps.some((l) => l.sport_units?.[key] != null));
  const hasKind = swim && laps.some((l) => l.extra?.intensityType);
  const rows = laps
    .map(
      (l) => `<tr>
        <td>${l.lap_index + lapOffset}</td>
        ${hasKind ? `<td>${l.extra?.intensityType === "REST" ? t("Rest") : l.extra?.intensityType ? t("Work") : "–"}</td>` : ""}
        <td class="num">${esc(clock(l.moving_s ?? l.elapsed_s))}</td>
        <td class="num">${l.distance_m ? esc(distanceText(sport, l.distance_m)) : "–"}</td>
        ${paced ? `<td class="num">${l.distance_m && isNum(l.avg_speed) ? `${esc(speedText(sport, l.avg_speed))} ${zoneBadge(zoneAt(paceScale, l.avg_speed))}` : "–"}</td>` : ""}
        <td class="num">${isNum(l.avg_hr) ? `${Math.round(l.avg_hr)} ${zoneBadge(zoneAt(hrScale, l.avg_hr))}` : "–"}</td>
        <td class="num">${isNum(l.avg_power) ? `${Math.round(l.avg_power)} W ${zoneBadge(zoneAt(powerScale, l.avg_power))}` : "–"}</td>
        ${shown.map(([key]) => `<td class="num">${l.sport_units?.[key] == null ? "–" : esc(key === "rest_s" ? clock(l.sport_units[key]) : String(l.sport_units[key]))}</td>`).join("")}
      </tr>`,
    )
    .join("");
  return `<h2>${t("Laps")}</h2><div class="table-wrap" tabindex="0" role="region" aria-label="${esc(t("Laps"))}"><table>
    <thead><tr><th>${swim ? t("Set") : t("Lap")}</th>${hasKind ? `<th>${t("Type")}</th>` : ""}<th class="num">${t("Time")}</th><th class="num">${t("Distance")}</th>${paced ? `<th class="num">${esc(speedLabel(sport))} (${esc(speedChannel(sport)[2])})</th>` : ""}<th class="num">${t("Avg HR")}</th><th class="num">${t("Power")}</th>${shown.map(([, label]) => `<th class="num">${esc(label)}</th>`).join("")}</tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

// Same zone number keeps the same colour across activity and training views; a shared
// number doesn't mean the same physiological load across heart rate, power and pace.
function zonesHtml(zones, sport) {
  return [...new Set(zones.map((z) => z.kind))].map((kind) => {
    const list = zones.filter((z) => z.kind === kind);
    const sources = [...new Set(list.map((z) => z.source).filter(Boolean))];
    return `<h2>${esc(zoneTitle(kind, sport))}</h2>${zoneRows(list,{kind,boundaries:true,sport})}${zoneTable(list,{kind,boundaries:true,sport})}
      <details><summary>${esc(t("Details"))}</summary><p class="sub">${esc(t("Source-reported zone times and lower boundaries."))}${sources.length ? ` ${esc(sources.map(sourceName).join(", "))}.` : ""}</p></details>`;
  }).join("");
}

function streamZoneLegend(zones, panels, sport) {
  return panels.map(([key, title, unit]) => {
    const kind = zoneKind(key);
    if (!["hr", "power", "pace"].includes(kind)) return "";
    const scale = zoneScale(zones, kind);
    return scale.length ? zoneLegend(scale, { kind, sport, title, unit }) : zoneMissing(title);
  }).filter(Boolean).join("");
}

function purposeLabel(value) {
  return ({
    training: "Training",
    recovery: "Recovery",
    leisure: "Leisure",
    coached_by_me: "Coached by me",
    race: "Race",
  })[value] || value;
}

function sourcesHtml(sources, a) {
  const rows = sources
    .map((s) => {
      const [source] = s.id.split(":");
      return `<tr><td>${esc(sourceName(source))}${s.id.startsWith(a.primary_source) ? ` <span class="chip">${t("main")}</span>` : ""}</td><td>${esc(s.name || "–")}</td><td class="muted">${esc(s.device || "")}</td></tr>`;
    })
    .join("");
  return `<h2>${t("Sources")}</h2><p class="sub" style="margin-top:0">${t("The same session as recorded by each service, merged into this one activity.")}</p>
    <div class="table-wrap"><table><tbody>${rows}</tbody></table></div>`;
}
