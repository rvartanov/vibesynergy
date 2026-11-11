// Best efforts: per sport or all together over a chosen period, with choosers that
// save both. Used by the start page and by Training → Fitness → Best efforts.
import { get, post } from "./api.js";
import { clock, esc, number, shortDate, sportName } from "./format.js";
import { N, t } from "./i18n.js";
import { speedText } from "./sport_units.js";

// Best running efforts ----------------------------------------------------------------------

export const BEST_PERIODS = [
  ["last_90d", N("Last 90 days")],
  ["last_365d", N("Last 12 months")],
  ["this_year", N("This year")],
  ["all", N("All time")],
];
const BEST_NAMES = { 400: "400 m", 1000: "1 km", 1609.34: N("Mile"), 5000: "5 km", 10000: "10 km", 21097.5: N("Half marathon") };

export const BEST_SPORTS = [
  ["run", N("Running")],
  ["bike", N("Cycling")],
  ["swim", N("Swimming")],
  ["other", N("Other sports")],
  ["all", N("All sports together")],
];

/** Best efforts over the saved period and sport, with choosers that save both. */
export function mountBests(host, { period, sport, canSave, stale, linkActivities = true }) {
  const chosen = BEST_PERIODS.some(([id]) => id === period) ? period : BEST_PERIODS[0][0];
  const chosenSport = BEST_SPORTS.some(([id]) => id === sport) ? sport : "run";
  host.innerHTML = `
    <div class="dash-bests-choice">
      <label class="dash-bests-period">${t("Sport")}
        <select class="btn" data-bests-sport>${BEST_SPORTS.map(([id, label]) => `<option value="${id}"${id === chosenSport ? " selected" : ""}>${esc(t(label))}</option>`).join("")}</select>
      </label>
      <label class="dash-bests-period">${t("Period")}
        <select class="btn" data-bests-period>${BEST_PERIODS.map(([id, label]) => `<option value="${id}"${id === chosen ? " selected" : ""}>${esc(t(label))}</option>`).join("")}</select>
      </label>
    </div>
    <p class="sub" data-bests-range aria-live="polite"></p>
    <div data-bests-table><div class="loading" role="status">${t("Reading activities…")}</div></div>`;
  const select = host.querySelector("[data-bests-period]");
  const sportSelect = host.querySelector("[data-bests-sport]");
  const load = async () => {
    const wanted = select.value;
    const wantedSport = sportSelect.value;
    const table = host.querySelector("[data-bests-table]");
    table.innerHTML = `<div class="loading" role="status">${t("Reading activities…")}</div>`;
    try {
      const data = wantedSport === "run"
        ? (await get("best-runs", { period: wanted })).data
        : (await get("bests", { sport: wantedSport, period: wanted })).data;
      if (stale() || !host.isConnected || select.value !== wanted || sportSelect.value !== wantedSport) return;
      host.querySelector("[data-bests-range]").textContent = t("From {start} to {end}.", {
        start: shortDate(data.start),
        end: shortDate(data.end),
      });
      table.innerHTML = wantedSport === "run" ? bestsTable(data, linkActivities) : otherBestsHtml(wantedSport, data, linkActivities);
    } catch (error) {
      if (!stale() && host.isConnected) table.innerHTML = `<p class="notice error" role="alert">${esc(t("This part could not load"))}: ${esc(error.message)}</p>`;
    }
  };
  select.addEventListener("change", () => {
    load();
    if (canSave) post("prefs", { changes: { best_runs_period: select.value } }).catch(() => {});
  });
  sportSelect.addEventListener("change", () => {
    load();
    if (canSave) post("prefs", { changes: { best_sport: sportSelect.value } }).catch(() => {});
  });
  load();
}

function activityCell(ref, linkActivities) {
  if (!ref) return "—";
  const name = esc(ref.name || sportName(ref.sport));
  const link = linkActivities && ref.activity ? `<a href="#/activity/${encodeURIComponent(ref.activity)}">${name}</a>` : name;
  return `${link} <span class="muted">${esc(shortDate(ref.date))}</span>`;
}

function durationLabel(seconds) {
  return seconds < 60 ? t("{n} s", { n: seconds }) : t("{n} min", { n: Math.round(seconds / 60) });
}

// One table: what, the value, and the activity it comes from. Missing values are "—".
function factsTable(caption, rows) {
  const body = rows
    .filter(([, value]) => value)
    .map(([label, value, ref]) => `<tr><th scope="row">${esc(label)}</th><td class="num">${esc(value)}</td><td>${ref}</td></tr>`)
    .join("");
  if (!body) return `<div class="empty">${esc(t("Nothing in this period yet."))}</div>`;
  return `<div class="table-wrap"><table aria-label="${esc(caption)}"><thead><tr><th scope="col">${esc(t("Best"))}</th><th scope="col" class="num">${esc(t("Value"))}</th><th scope="col">${esc(t("Activity"))}</th></tr></thead><tbody>${body}</tbody></table></div>`;
}

function longestRows(block, link) {
  const time = block?.longest_time;
  const distance = block?.longest_distance;
  return [
    [t("Longest by time"), time ? clock(time.seconds) : "", activityCell(time, link)],
    [t("Longest by distance"), distance ? `${number(distance.metres / 1000, 1)} km` : "", activityCell(distance, link)],
  ];
}

function otherBestsHtml(sport, data, link) {
  if (sport === "bike") {
    const bike = data.bike || {};
    const power = (bike.power || []).map((p) => [durationLabel(p.seconds), `${number(p.watts)} W`, activityCell(p, link)]);
    return factsTable(t("Best cycling efforts"), [...power, ...longestRows(bike, link)]);
  }
  if (sport === "swim") {
    const swim = data.swim || {};
    const fast = swim.fastest_session;
    return factsTable(t("Best swims"), [
      [t("Fastest session (from 1000 m)"), fast ? t("{pace} per 100 m", { pace: clock(fast.s_per_100m) }) : "", activityCell(fast, link)],
      ...longestRows(swim, link),
    ]);
  }
  if (sport === "other") return factsTable(t("Other sports"), longestRows(data.other, link));
  const all = data.together || {};
  const week = all.most_hours_week;
  const day = all.biggest_day_load;
  return factsTable(t("All sports together"), [
    ...longestRows(all, link),
    [t("Most hours in a week"), week ? clock(week.seconds) : "", week ? esc(t("week of {date}", { date: shortDate(week.monday) })) : ""],
    [t("Biggest load in a day"), day ? number(day.load) : "", day ? esc(shortDate(day.date)) : ""],
  ]);
}

function bestsTable(data, linkActivities) {
  if (!data.curve.length) return `<div class="empty">${t("No runs with distance data in this period.")}</div>`;
  const rows = data.curve.map((b) => {
    const name = esc(b.name || sportName(b.sport));
    const activity = linkActivities ? `<a href="#/activity/${encodeURIComponent(b.activity)}">${name}</a>` : name;
    return `<tr><th scope="row">${esc(BEST_NAMES[b.distance_m] ? t(BEST_NAMES[b.distance_m]) : `${number(b.distance_m)} m`)}</th>
      <td class="num">${esc(clock(b.seconds))}</td>
      <td class="num">${esc(speedText("running", b.distance_m / b.seconds))}</td>
      <td>${activity} <span class="muted">${esc(shortDate(b.date))}</span></td></tr>`;
  }).join("");
  return `<div class="table-wrap"><table aria-label="${esc(t("Best running efforts"))}"><thead><tr><th scope="col">${t("Distance")}</th><th scope="col" class="num">${t("Time")}</th><th scope="col" class="num">${t("Pace")}</th><th scope="col">${t("Activity")}</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="sub">${esc(t("Based on {n} runs. Parts faster than running (bike, car) are left out.", { n: number(data.activities_with_distance) }))}</p>`;
}

