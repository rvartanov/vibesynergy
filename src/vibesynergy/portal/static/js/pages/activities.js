// Activities: one row per real session (merged across sources), filterable, with totals for
// the whole match. Filters live in the address, so a filtered list can be bookmarked.
import { get } from "../api.js";
import {
  esc,
  hoursMinutes,
  isNum,
  km,
  number,
  shortDate,
  weekday,
  sourceName,
  sportName,
  timeOfDay,
} from "../format.js";
import { N, t } from "../i18n.js";
import {
  apiParams,
  bindPeriodPicker,
  isoToday,
  mergePeriodValue,
  periodPickerHtml,
  saveStored,
  writePeriodQuery,
} from "../period_picker.js";
import { averageSpeed, distanceText, speedText } from "../sport_units.js";
import { sportChip } from "../symbols.js";

const STORAGE = "vibesynergy.period.activities";
const DEFAULT = { period: "all" };
const EXTRA = [["all", N("All time")]];

const SORTS = [
  ["date", N("Newest first")],
  ["distance", N("Longest distance")],
  ["duration", N("Longest time")],
  ["load", N("Highest load")],
  ["ascent", N("Most climbing")],
  ["avg_hr", N("Highest average HR")],
];

const PAGE = 50;
export async function render(view, { query, meta, stale }) {
  const today = meta?.data?.today || isoToday();
  let periodValue = mergePeriodValue(query, STORAGE, DEFAULT, { today });
  const filters = {
    sport: query.get("sport") || "",
    text: query.get("text") || "",
    sort: query.get("sort") || "date",
  };
  const reply = await get("activities", { ...apiParams(periodValue, today), ...filters, limit: PAGE, offset: 0 });
  if (stale()) return;
  const tz = reply.meta.timezone;
  document.title = `${t("Activities")} · VibeSynergy`;
  const { data } = reply;

  view.innerHTML = `
    <div class="page-head"><h1>${t("Activities")}</h1></div>
    <form class="filters" role="search">
      ${periodPickerHtml({ id: "period", value: periodValue, extra: EXTRA, today })}
      <label>${t("Sport")} ${select("sport", [["", t("All sports")], ...data.sports.map((s) => [s.sport, `${sportName(s.sport)} (${s.count})`])], filters.sport)}</label>
      <label>${t("Sort")} ${select("sort", SORTS, filters.sort)}</label>
      <label class="grow">${t("Search")} <input class="btn" type="search" name="text" value="${esc(filters.text)}" placeholder="${esc(t("Name contains…"))}"></label>
    </form>
    <div class="sub totals">${totalsText(data.totals)}</div>
    <div class="card">
      ${data.activities.length ? tableHtml(data.activities, tz) : `<div class="empty">${t("No activities match these filters.")}</div>`}
      <div class="more"></div>
    </div>`;

  const form = view.querySelector("form");
  const apply = () => {
    const params = new URLSearchParams();
    for (const [key, value] of new FormData(form)) {
      if (value && key !== "period-preset" && !key.startsWith("period-") && !(key === "sort" && value === "date")) {
        params.set(key, value);
      }
    }
    writePeriodQuery(params, periodValue, { defaultValue: DEFAULT });
    const qs = params.toString();
    location.hash = `#/activities${qs ? `?${qs}` : ""}`;
  };
  bindPeriodPicker(view, {
    id: "period",
    value: periodValue,
    extra: EXTRA,
    today,
    onChange: (next) => {
      saveStored(STORAGE, next);
      periodValue = next;
      apply();
    },
  });
  form.addEventListener("change", (event) => {
    if (event.target.name?.startsWith("period-")) return;
    apply();
  });
  view.querySelector("table.activities")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-parts-for]");
    if (!button) return;
    const open = button.getAttribute("aria-expanded") !== "true";
    button.setAttribute("aria-expanded", String(open));
    for (const row of view.querySelectorAll(`tr[data-part-of="${CSS.escape(button.dataset.partsFor)}"]`)) row.hidden = !open;
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    apply();
  });

  let next = data.next_offset;
  const more = view.querySelector(".more");
  const showMore = () => {
    more.innerHTML = next === null ? "" : `<button class="btn" type="button">${esc(t("Show {n} more", { n: PAGE }))}</button>`;
    more.querySelector("button")?.addEventListener("click", async () => {
      const page = await get("activities", { ...apiParams(periodValue, today), ...filters, limit: PAGE, offset: next });
      view.querySelector("tbody").insertAdjacentHTML("beforeend", rowsHtml(page.data.activities, tz));
      next = page.data.next_offset;
      showMore();
    });
  };
  showMore();
}

function select(name, options, current) {
  return `<select class="btn" name="${name}">${options
    .map(([value, label]) => `<option value="${esc(value)}"${value === current ? " selected" : ""}>${esc(t(label))}</option>`)
    .join("")}</select>`;
}

function totalsText(totals) {
  const parts = [totals.count === 1 ? t("1 activity") : t("{n} activities", { n: number(totals.count) })];
  if (totals.moving_s) parts.push(hoursMinutes(totals.moving_s));
  if (totals.distance_m) parts.push(km(totals.distance_m));
  if (totals.ascent_m) parts.push(t("{n} m climbing", { n: number(totals.ascent_m) }));
  return esc(parts.join(" · "));
}

function tableHtml(activities, tz) {
  return `<div class="table-wrap"><table class="activities">
    <thead><tr>
      <th>${t("Date")}</th><th>${t("Activity")}</th><th class="num">${t("Time")}</th><th class="num">${t("Distance")}</th>
      <th class="num wide">${t("Pace / speed")}</th><th class="num wide">${t("Power")}</th><th class="num wide">${t("Avg HR")}</th><th class="num wide">${t("Climb")}</th><th class="wide">${t("Sources")}</th>
    </tr></thead>
    <tbody>${rowsHtml(activities, tz)}</tbody></table></div>`;
}

// Parts follow their holder; a part whose holder is not in this batch stays a row.
function ordered(activities) {
  const byId = new Map(activities.map((a) => [a.group_id, a]));
  const held = new Set(activities.filter((a) => a.part_of && byId.has(a.part_of)).map((a) => a.group_id));
  const out = [];
  for (const a of activities) {
    if (held.has(a.group_id)) continue;
    out.push(a);
    for (const id of a.parts || []) if (byId.has(id)) out.push(byId.get(id));
  }
  return out.map((a) => (a.part_of && !byId.has(a.part_of) ? { ...a, part_of: null } : a));
}

function rowsHtml(activities, tz) {
  return ordered(activities)
    .map((a) => {
      const id = a.sources?.[0] ?? a.group_id;
      // A multisport holder folds its parts away; a part sits indented under it.
      const holder = a.parts?.length
        ? ` <button class="btn small" type="button" aria-expanded="false" data-parts-for="${esc(a.group_id)}">${esc(t("{n} parts", { n: a.parts.length }))}</button>`
        : "";
      const sportText = a.parts?.length ? t("Multisport") : sportName(a.sport);
      const speed = a.distance_m ? averageSpeed(a) : null;
      const rowAttrs = a.part_of ? ` class="is-part" data-part-of="${esc(a.part_of)}" hidden` : a.parts?.length ? ' class="is-multisport"' : "";
      return `<tr${rowAttrs}>
        <td class="date"><span class="wide">${esc(weekday(a.local_date))} </span>${esc(shortDate(a.local_date))} <span class="sub act-time">${esc(timeOfDay(a.start_utc, tz))}</span></td>
        <td><span class="act-name">${a.part_of ? '<span class="part-mark" aria-hidden="true">↳ </span>' : ""}${sportChip(a.sport, sportText, { hideName: true })}<a href="#/activity/${encodeURIComponent(id)}">${esc(a.name || sportName(a.sport))}</a>${holder}</span></td>
        <td class="num">${esc(hoursMinutes(a.moving_s ?? a.elapsed_s))}</td>
        <td class="num">${a.distance_m ? esc(distanceText(a.sport, a.distance_m)) : "–"}</td>
        <td class="num wide">${speed ? esc(speedText(a.sport, speed)) : "–"}</td>
        <td class="num wide">${a.sport_units?.power ? esc(`${a.sport_units.power}${a.sport_units.power_per_kg ? ` · ${a.sport_units.power_per_kg}` : ""}`) : "–"}</td>
        <td class="num wide">${isNum(a.avg_hr) ? `${Math.round(a.avg_hr)}` : "–"}</td>
        <td class="num wide">${isNum(a.ascent_m) && a.ascent_m > 0 ? `${number(a.ascent_m)} m` : "–"}</td>
        <td class="muted wide">${esc((a.sources || []).map((s) => sourceName(s.split(":")[0])).join(" · "))}</td>
      </tr>`;
    })
    .join("");
}
