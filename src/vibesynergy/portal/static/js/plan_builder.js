// Planning & progress → Build a plan: what a new plan is built for and how. The goal
// is one of your races or a new one; the sport is running or triathlon, every distance or a
// custom one, and its type. Filled in from the current plan; every change saves at once.
import { get, post } from "./api.js";
import { esc, hoursMinutes, shortDate, weekday } from "./format.js";
import { N, t } from "./i18n.js";

const LABELS = {
  run: N("Running"),
  triathlon: N("Triathlon"),
  "5k": "5 km",
  "10k": "10 km",
  half: N("Half marathon"),
  marathon: N("Marathon"),
  ultra: N("Ultra"),
  super_sprint: N("Super sprint"),
  sprint: N("Sprint"),
  olympic: N("Olympic"),
  middle: N("Middle distance (70.3)"),
  full: N("Full distance"),
  custom: N("Custom"),
  road: N("Road"),
  trail: N("Trail"),
  cross: N("Cross"),
  offroad: N("Off-road"),
  friel: N("Friel periodisation"),
  polarised: N("Polarised (80/20)"),
  pyramidal: N("Pyramidal"),
  "3:1": N("3 load weeks, 1 recovery week"),
  "2:1": N("2 load weeks, 1 recovery week"),
};
const label = (key) => t(LABELS[key] || key);
const DAYS = [N("Monday"), N("Tuesday"), N("Wednesday"), N("Thursday"), N("Friday"), N("Saturday"), N("Sunday")];
const FIXED_SPORTS = ["run", "ride", "swim", "strength"];

// When you train (rest days and fixed moments such as a group training.
function whenHtml(s) {
  const rest = new Set(s.rest_days || []);
  const days = DAYS.map((d, i) => `<label class="check"><input type="checkbox" name="rest_day" value="${i}"${rest.has(i) ? " checked" : ""}> <span>${esc(t(d))}</span></label>`).join("");
  const rows = (s.fixed || []).map((m, i) => `<fieldset class="settings-field" data-fixed="${i}"><legend>${esc(t("Fixed moment {n}", { n: i + 1 }))}</legend>
      <label>${esc(t("Day"))} <select class="btn" name="fixed_day">${DAYS.map((d, j) => `<option value="${j}"${j === m.day ? " selected" : ""}>${esc(t(d))}</option>`).join("")}</select></label>
      <label>${esc(t("Sport"))} <select class="btn" name="fixed_sport">${FIXED_SPORTS.map((x) => `<option value="${x}"${x === m.sport ? " selected" : ""}>${esc(t(SPORT_NAMES[x]))}</option>`).join("")}</select></label>
      <label>${esc(t("Name"))} <input class="btn" name="fixed_name" maxlength="80" value="${esc(m.name)}"></label>
      <label>${esc(t("Minutes"))} <input class="btn" type="number" min="10" max="300" step="5" name="fixed_minutes" value="${esc(m.minutes)}"></label>
      <button class="btn" type="button" data-fixed-remove="${i}">${esc(t("Remove"))}</button>
    </fieldset>`).join("");
  return `<h2>${esc(t("When you train"))}</h2>
    <p class="sub">${esc(t("Days you never train and sessions that are always there, like a group training. You can change this at any time, also while the plan runs."))}</p>
    <fieldset class="day-picks"><legend>${esc(t("Rest days"))}</legend>${days}</fieldset>
    <div class="settings-grid">${rows}</div>
    ${(s.fixed || []).length < 10 ? `<button class="btn" type="button" data-fixed-add>${esc(t("Add a fixed moment"))}</button>` : ""}`;
}

function options(values, current) {
  return values.map((v) => `<option value="${esc(v)}"${String(v) === String(current) ? " selected" : ""}>${esc(label(v))}</option>`).join("");
}

function formHtml(data) {
  const s = data.settings;
  const c = data.choices;
  const sport = c.sports[s.sport] || c.sports.triathlon;
  const goalValue = s.goal?.new ? "new" : s.goal?.race || "";
  const races = data.races.map((r) => `<option value="${esc(r.id)}"${r.id === goalValue ? " selected" : ""}>${esc(`${r.title} · ${r.date}${r.priority ? ` (${r.priority})` : ""}`)}</option>`).join("");
  const isNew = goalValue === "new";
  const custom = s.custom || {};
  const customFields = s.distance === "custom"
    ? (s.sport === "triathlon"
      ? [["swim_km", t("Swim (km)")], ["bike_km", t("Bike (km)")], ["run_km", t("Run (km)")]]
      : [["run_km", t("Distance (km)")]])
        .map(([k, l]) => `<label>${esc(l)} <input class="btn" type="number" min="0.1" step="0.1" name="${k}" value="${esc(custom[k] ?? "")}"></label>`).join("")
    : "";
  return `<form class="card settings-section" data-builder novalidate>
    <h2>${esc(t("Goal"))}</h2>
    <div class="settings-grid">
      <label>${esc(t("Race"))} <select class="btn" name="goal">${races}<option value="new"${isNew ? " selected" : ""}>${esc(t("A new race…"))}</option></select></label>
      ${isNew ? `<label>${esc(t("Name"))} <input class="btn" name="new_name" value="${esc(s.goal.new.name || "")}"></label>
        <label>${esc(t("Date"))} <input class="btn" type="date" name="new_date" value="${esc(s.goal.new.date || "")}"></label>
        <label>${esc(t("Priority"))} <select class="btn" name="new_priority">${c.priorities.map((p) => `<option${p === s.goal.new.priority ? " selected" : ""}>${p}</option>`).join("")}</select></label>` : ""}
    </div>
    <h2>${esc(t("Sport"))}</h2>
    <div class="settings-grid">
      <label>${esc(t("Sport"))} <select class="btn" name="sport">${options(Object.keys(c.sports), s.sport)}</select></label>
      <label>${esc(t("Distance"))} <select class="btn" name="distance">${options(sport.distances, s.distance)}</select></label>
      <label>${esc(t("Type"))} <select class="btn" name="type">${options(sport.types, s.type)}</select></label>
      ${customFields}
    </div>
    <h2>${esc(t("How"))}</h2>
    <div class="settings-grid">
      <label>${esc(t("Method"))} <select class="btn" name="method">${options(c.methods, s.method)}</select></label>
      <label>${esc(t("Hours per week, at least"))} <input class="btn" type="number" min="1" max="40" step="0.5" name="hours_min" value="${esc(s.hours_min)}"></label>
      <label>${esc(t("Hours per week, at most"))} <input class="btn" type="number" min="1" max="40" step="0.5" name="hours_max" value="${esc(s.hours_max)}"></label>
      <label>${esc(t("Rhythm"))} <select class="btn" name="rhythm">${options(c.rhythms, s.rhythm)}</select></label>
      <label>${esc(t("Taper (weeks)"))} <input class="btn" type="number" min="1" max="4" step="1" name="taper_weeks" value="${esc(s.taper_weeks)}"></label>
      <label class="check"><input type="checkbox" name="doubles"${s.doubles ? " checked" : ""}> <span>${esc(t("Two sessions on one day are fine"))}</span></label>
      <label class="check"><input type="checkbox" name="strength"${s.strength ? " checked" : ""}> <span>${esc(t("Strength training"))}</span></label>
      <label class="check"><input type="checkbox" name="tests"${s.tests ? " checked" : ""}> <span>${esc(t("Test weeks (FTP, CSS, run test)"))}</span></label>
    </div>
    ${whenHtml(s)}
    <p class="sub" role="status" data-builder-status></p>
  </form>
  <section class="card" aria-labelledby="builder-draft-title">
    <h2 id="builder-draft-title">${esc(t("Draft plan"))}</h2>
    <p class="sub">${esc(t("A first plan from these settings, to look at. Nothing changes in your plan; your coach fine-tunes it."))}</p>
    <button class="btn primary" type="button" data-builder-draft>${esc(t("Make a draft"))}</button>
    <p class="sub" role="status" data-draft-status></p>
    <div data-draft></div>
  </section>`;
}

function collect(form, sports) {
  const f = (name) => form.elements[name];
  const goal = f("goal").value === "new"
    ? { new: { name: f("new_name")?.value || "", date: f("new_date")?.value || "", priority: f("new_priority")?.value || "A" } }
    : { race: f("goal").value };
  const sport = f("sport").value;
  const distances = sports[sport].distances;
  const types = sports[sport].types;
  const out = {
    goal,
    sport,
    distance: distances.includes(f("distance").value) ? f("distance").value : distances[distances.length - 2],
    type: types.includes(f("type").value) ? f("type").value : types[0],
    method: f("method").value,
    hours_min: Number(f("hours_min").value),
    hours_max: Number(f("hours_max").value),
    rhythm: f("rhythm").value,
    taper_weeks: Number(f("taper_weeks").value),
    doubles: f("doubles").checked,
    strength: f("strength").checked,
    tests: f("tests").checked,
  };
  out.rest_days = [...form.querySelectorAll('[name="rest_day"]:checked')].map((box) => Number(box.value));
  out.fixed = [...form.querySelectorAll("[data-fixed]")].map((row) => ({
    day: Number(row.querySelector('[name="fixed_day"]').value),
    sport: row.querySelector('[name="fixed_sport"]').value,
    name: row.querySelector('[name="fixed_name"]').value.trim() || t("Fixed moment"),
    minutes: Math.round(Number(row.querySelector('[name="fixed_minutes"]').value) || 60),
  }));
  const custom = {};
  for (const key of ["swim_km", "bike_km", "run_km"]) if (f(key)?.value) custom[key] = Number(f(key).value);
  if (Object.keys(custom).length) out.custom = custom;
  return out;
}

const PHASES = { base: N("Base"), build: N("Build"), peak: N("Peak"), taper: N("Taper"), race: N("Race week") };
const KINDS = { load: N("load week"), recovery: N("recovery week"), taper: N("taper"), race: N("race") };
const SPORT_NAMES = { swim: N("Swim"), ride: N("Bike"), run: N("Run"), strength: N("Strength") };

function draftHtml(d) {
  const rows = d.weeks.map((w) => {
    const total = w.sessions.reduce((sum, x) => sum + x.minutes, 0);
    const split = Object.entries(w.sports).map(([sport, h]) => `${t(SPORT_NAMES[sport] || sport)} ${hoursMinutes(h * 3600)}`).join(" · ");
    const sessions = w.sessions.map((x) => `<li><span class="sub">${esc(weekday(x.date))}</span> ${esc(x.name)} <span class="sub">${esc(hoursMinutes(x.minutes * 60))}${x.fixed ? ` · ${esc(t("fixed"))}` : x.moved ? ` · ${esc(t("moved from a rest day"))}` : ""}</span></li>`).join("");
    const now = w.current
      ? ` <span class="sub">· ${esc(t("now: {phase}, {hours}", { phase: w.current.phase || "–", hours: w.current.hours ? hoursMinutes(w.current.hours * 3600) : "–" }))}</span>`
      : "";
    return `<li><details class="collapsible"><summary><b>${esc(shortDate(w.start))}</b> · ${esc(t(PHASES[w.phase] || w.phase))}, ${esc(t(KINDS[w.kind] || w.kind))} · ${esc(hoursMinutes(total * 60))}${now}</summary>
      <p class="sub">${esc(split)}</p><ul class="plain">${sessions}</ul></details></li>`;
  }).join("");
  return `<p>${esc(t("{n} weeks from {start} to the race on {race}.", { n: d.weeks.length, start: shortDate(d.start), race: shortDate(d.race_date) }))}</p><ul class="plain">${rows}</ul>`;
}

export async function mountBuilder(host, stale) {
  let { data } = await get("plan/builder");
  if (stale() || !host.isConnected) return;
  const draw = (current) => {
    host.innerHTML = formHtml(current);
    const form = host.querySelector("[data-builder]");
    form.addEventListener("change", async () => {
      const status = form.querySelector("[data-builder-status]");
      const settings = collect(form, current.choices.sports);
      if (settings.goal.new && (!settings.goal.new.name || !settings.goal.new.date)) {
        draw({ ...current, settings });  // show the name and date fields first
        return;
      }
      try {
        await post("prefs", { changes: { plan_builder: settings } });
        const redraw = settings.sport !== current.settings.sport || settings.distance !== current.settings.distance || Boolean(settings.goal.new) !== Boolean(current.settings.goal?.new);
        current = { ...current, settings };
        if (redraw) draw(current);
        else status.textContent = t("Saved.");
      } catch (error) {
        status.textContent = t("Not saved: {error}", { error: error.message });
      }
    });
  };
  const save = async (current, settings) => {
    await post("prefs", { changes: { plan_builder: settings } });
    return { ...current, settings };
  };
  draw(data);
  host.addEventListener("click", async (event) => {
    const add = event.target.closest("[data-fixed-add]");
    const remove = event.target.closest("[data-fixed-remove]");
    if (add || remove) {
      const form = host.querySelector("[data-builder]");
      const settings = collect(form, data.choices.sports);
      if (add) settings.fixed.push({ day: 1, sport: "run", name: t("Group training"), minutes: 90 });
      else settings.fixed.splice(Number(remove.dataset.fixedRemove), 1);
      try {
        data = await save(data, settings);
        draw(data);
        host.querySelector(add ? "[data-fixed]:last-of-type [name=fixed_name]" : "[data-fixed-add]")?.focus();
      } catch (error) {
        host.querySelector("[data-builder-status]").textContent = t("Not saved: {error}", { error: error.message });
      }
      return;
    }
    if (!event.target.closest("[data-builder-draft]")) return;
    const status = host.querySelector("[data-draft-status]");
    status.textContent = t("Making the draft…");
    try {
      const { data: d } = await get("plan/builder/draft");
      if (stale() || !host.isConnected) return;
      host.querySelector("[data-draft]").innerHTML = draftHtml(d);
      status.textContent = "";
    } catch (error) {
      status.textContent = t(error.message);
    }
  });
}
