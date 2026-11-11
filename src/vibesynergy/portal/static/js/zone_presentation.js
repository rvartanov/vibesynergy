// Shared ordinal zone roles. Source boundaries remain independent for each metric/sport.
// Heart rate and power stay in their recorded units (bpm, W); pace is recorded as speed
// (m/s) and only converted to a pace/speed string for display, with the athlete's sport.
// Colours always come from --zone-N (the design tokens). Do not substitute series colours.
import { esc, hoursMinutes, isNum, number } from "./format.js";
import { t } from "./i18n.js";
import { speedText, speedUnit } from "./sport_units.js";

export function zoneToken(zone) {
  const n = Number(zone);
  return Number.isInteger(n) && n >= 0 && n <= 7 ? `zone-${n}` : "zone-other";
}

// Short ordinal shown next to the swatch (Z1…Zn). The spoken name stays "Zone n".
export function zoneLabel(zone) {
  return t("Z{n}", { n: zone });
}

export function zoneName(zone) {
  return t("Zone {n}", { n: zone });
}

// Swatch plus Z-label. The code is visual; the name is what a screen reader hears.
function zoneMark(zone) {
  return `<i class="zone-swatch" style="background:var(--${zoneToken(zone)})" aria-hidden="true"></i><span class="zone-code" aria-hidden="true">${esc(zoneLabel(zone))}</span><span class="visually-hidden">${esc(zoneName(zone))}</span>`;
}

export function zoneBadge(zone) {
  const n = Number(zone);
  if (zone === null || zone === undefined || zone === "" || !Number.isInteger(n)) return "";
  return `<span class="zone-badge">${zoneMark(n)}</span>`;
}

// Complete consecutive activity boundaries only; never colour from a partial/malformed set.
// Pace zones come from the device's own recorded zone boundaries (FIT), never derived from
// heart rate or power.
export function zoneScale(rows, kind) {
  if (!["hr", "power", "pace"].includes(kind)) return [];
  const list = rows.filter((z) => z.kind === kind).sort((a, b) => a.zone - b.zone);
  if (list.length < 2 || ![0, 1].includes(list[0].zone)) return [];
  if (list.some((z, i) => !Number.isInteger(z.zone) || !isNum(z.low) || z.low < 0 ||
    (i > 0 && (z.zone !== list[i - 1].zone + 1 || z.low <= list[i - 1].low)))) return [];
  return list.map((z, i) => ({ zone: z.zone, low: z.low, high: list[i + 1]?.low ?? null }));
}

export function zoneAt(scale, value) {
  if (!isNum(value)) return null;
  return scale.find((z) => value >= z.low && (z.high === null || value < z.high))?.zone ?? null;
}

export function zoneTitle(kind, sport) {
  if (kind === "hr") return t("Time in heart rate zones");
  if (kind === "power") return t("Time in power zones");
  if (kind === "pace") return t(speedUnit(sport) === "km/h" ? "Time in speed zones" : "Time in pace zones");
  return t("Time in zones ({kind})", {kind});
}

// A zone's lower boundary, in words: heart rate and power read as a plain threshold;
// pace/speed reads as "this fast or faster" because a higher zone number means a faster
// pace (a lower minutes-per-distance value), not a higher raw number.
export function zoneBoundaryText(kind, sport, low) {
  if (!isNum(low) || (kind === "pace" && low <= 0)) return "";
  if (kind === "pace") {
    const text = speedText(sport, low);
    return text === "–" ? "" : t("{pace} or faster", { pace: text });
  }
  const unit = kind === "hr" ? "bpm" : kind === "power" ? "W" : "";
  return unit ? `≥ ${number(low)} ${unit}` : "";
}

// One ordered view for charts, legends, badges and tables. Zone count is the recorded
// count: seven power zones stay seven, and a missing boundary is not invented.
export function presentZones(rows, { kind = "hr", sport } = {}) {
  const list = [...rows]
    .filter((z) => z.kind == null || z.kind === kind)
    .sort((a, b) => Number(a.zone) - Number(b.zone));
  const total = list.reduce((sum, z) => sum + (isNum(z.seconds) ? Math.max(0, z.seconds) : 0), 0);
  const scale = zoneScale(list.map((z) => ({ ...z, kind: z.kind || kind })), kind);
  const bands = new Map(scale.map((z) => [z.zone, z]));
  return {
    kind,
    sport: sport || "",
    title: zoneTitle(kind, sport),
    totalSeconds: total,
    configured: scale.length > 0,
    zones: list.map((z) => {
      const band = bands.get(Number(z.zone));
      const low = band ? band.low : isNum(z.low) ? z.low : null;
      const seconds = isNum(z.seconds) ? Math.max(0, z.seconds) : null;
      return {
        zone: Number(z.zone),
        label: zoneLabel(z.zone),
        name: zoneName(z.zone),
        token: zoneToken(z.zone),
        low,
        high: band ? band.high : null,
        boundary: zoneBoundaryText(kind, sport, low),
        seconds,
        share: total && seconds !== null ? seconds / total : 0,
      };
    }),
  };
}

export function zoneRows(rows, { kind = "hr", boundaries = false, sport } = {}) {
  const view = presentZones(rows, { kind, sport });
  return `<div class="zones" role="list">${view.zones.map((z) => {
    const pct = Math.round(z.share * 100);
    const bound = boundaries && z.boundary ? `<span class="zone-bound">${esc(z.boundary)}</span>` : "";
    return `<div class="zone" role="listitem"><span class="zlabel">${zoneBadge(z.zone)}${bound}</span>
      <span class="ztrack" aria-hidden="true"><span style="width:${pct}%;background:var(--${z.token})"></span></span>
      <span class="zvalue">${esc(hoursMinutes(z.seconds))} <span class="sub">${pct}%</span></span></div>`;
  }).join("")}</div>`;
}

export function zoneTable(rows, { kind = "hr", boundaries = false, sport } = {}) {
  const view = presentZones(rows, { kind, sport });
  if (!view.zones.length) return "";
  const boundHead = boundaries ? `<th scope="col">${esc(t("Boundary"))}</th>` : "";
  return `<div class="table-wrap zone-table" tabindex="0" role="region" aria-label="${esc(view.title)}"><table>
    <caption class="visually-hidden">${esc(view.title)}</caption>
    <thead><tr><th scope="col">${esc(t("Zone"))}</th>${boundHead}<th scope="col">${esc(t("Time"))}</th><th scope="col">${esc(t("Share"))}</th></tr></thead>
    <tbody>${view.zones.map((z) => `<tr><th scope="row">${zoneBadge(z.zone)}</th>${boundaries ? `<td>${esc(z.boundary || "–")}</td>` : ""}<td class="num">${esc(hoursMinutes(z.seconds))}</td><td class="num">${Math.round(z.share * 100)}%</td></tr>`).join("")}</tbody></table></div>`;
}

export function zoneLegend(scale, { kind, sport, title, unit } = {}) {
  if (!scale?.length) return "";
  const heading = `${title || zoneTitle(kind, sport)}${unit ? ` (${unit})` : ""}`;
  return `<div class="stream-zone-key"><strong>${esc(heading)}</strong><div class="legend zone-legend">${scale.map((z) => {
    const text = zoneBoundaryText(kind, sport, z.low);
    return `<span class="zone-legend-item">${zoneBadge(z.zone)}${text ? `<span class="zone-legend-bound">${esc(text)}</span>` : ""}</span>`;
  }).join("")}</div></div>`;
}

export function zoneMissing(metricLabel) {
  return `<p class="zone-missing"><strong>${esc(t("Zones not configured"))}</strong> ${esc(t("The chart still shows {metric}.", { metric: metricLabel }))} <a href="#/settings/data/sources">${esc(t("Check sources"))}</a></p>`;
}
