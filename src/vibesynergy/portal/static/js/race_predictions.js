// Fitness → Form: predicted race times for 5 km to 50 km (#9), from one hour at your
// threshold pace or a recent effort, each with how certain it is, the change against six
// weeks ago and Garmin's prediction next to it. Fetched on its own, so the chart doesn't wait.
import { get } from "./api.js";
import { clock, esc, userDate } from "./format.js";
import { N, t } from "./i18n.js";

const NAMES = {
  "5k": N("5 km"),
  "10k": N("10 km"),
  "10mi": N("10 miles"),
  half: N("Half marathon"),
  marathon: N("Marathon"),
  "50k": N("50 km"),
};
const CERTAINTY = { high: N("certain"), medium: N("fairly certain"), low: N("rough") };

function change(seconds) {
  if (seconds == null) return "–";
  if (Math.abs(seconds) < 5) return t("same");
  const text = clock(Math.abs(seconds));
  return seconds < 0 ? t("{time} faster", { time: text }) : t("{time} slower", { time: text });
}

function basisLine(rows) {
  const from = rows.find((r) => r.from)?.from;
  if (!from) return "";
  if (from.kind === "threshold") {
    return t("From your threshold pace ({pace} per km, set {date}): an hour at that pace is the starting point. Rested and on a good day, a race can go a little faster.", {
      pace: clock(3600 / (from.distance_m / 1000)),
      date: userDate(from.date),
    });
  }
  return t("From your fastest recent run over {km} km ({date}). Training runs are usually slower than racing, so these times are on the safe side.", {
    km: (from.distance_m / 1000).toFixed(1),
    date: userDate(from.date),
  });
}

export function predictionsHtml(data) {
  const rows = (data?.predictions || []).filter((r) => r.seconds);
  if (!rows.length) {
    return `<p class="empty">${esc(t("No prediction yet: set your threshold pace under Zones & thresholds, or run a test."))} <a href="#/zones">${esc(t("Zones & thresholds"))}</a></p>`;
  }
  const body = rows
    .map(
      (r) => `<tr><th scope="row">${esc(t(NAMES[r.key] || r.key))}</th>
        <td><b>${esc(clock(r.seconds))}</b> <span class="chip" data-certainty="${esc(r.certainty)}">${esc(t(CERTAINTY[r.certainty] || r.certainty))}</span></td>
        <td>${esc(clock(r.pace_s_per_km))}/km</td>
        <td>${esc(change(r.change_s))}</td>
        <td>${r.garmin ? esc(clock(r.garmin.seconds)) : "–"}</td></tr>`,
    )
    .join("");
  return `<p class="sub">${esc(basisLine(rows))}</p>
    <div class="table-wrap"><table>
      <thead><tr><th scope="col">${esc(t("Distance"))}</th><th scope="col">${esc(t("Predicted"))}</th><th scope="col">${esc(t("Pace"))}</th><th scope="col">${esc(t("Since 6 weeks ago"))}</th><th scope="col">Garmin</th></tr></thead>
      <tbody>${body}</tbody>
    </table></div>
    <details class="details"><summary>${esc(t("How the prediction is made"))}</summary>
      <p class="sub">${esc(t("Each distance is predicted with Riegel's formula from one hour at your threshold pace and from your fastest runs of the last 12 weeks; the fastest prediction counts. Beyond the half marathon the formula is made a bit stricter, because the marathon is harder than it predicts for most runners. How certain it is depends on how far it reaches beyond its starting point and how old that is."))}</p>
    </details>`;
}

export async function mountPredictions(host, { stale = () => false } = {}) {
  if (!host) return;
  try {
    const reply = await get("race-predictions");
    if (stale() || !host.isConnected) return;
    host.innerHTML = predictionsHtml(reply.data);
  } catch (error) {
    if (!stale() && host.isConnected) host.innerHTML = `<p class="notice error" role="alert">${esc(t("Could not load: {error}", { error: error.message }))}</p>`;
  }
}
