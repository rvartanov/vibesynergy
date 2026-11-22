// Races and events on Planning & progress (#91): the races with a countdown and the
// projected race-day form, the other events that matter to the plan (group training, camp,
// holiday, not available), and one form to add, change or delete either. The server checks
// it with the same function as the MCP (query/race_events.py), refuses a second copy of
// one that is already there, and copies it to intervals.icu. Saves at once; a delete can
// be undone from the status line.
import { get, post } from "./api.js";
import { esc, isNum, longDate, number } from "./format.js";
import { N, t } from "./i18n.js";
import { zoneText } from "./form_block.js";

const KIND_LABELS = {
  race: N("Race"),
  group_training: N("Group training"),
  camp: N("Training camp"),
  holiday: N("Holiday"),
  unavailable: N("Not available"),
  other: N("Other event"),
};
const SPORT_LABELS = {
  run: N("Running"),
  trail: N("Trail running"),
  ride: N("Cycling"),
  swim: N("Swimming"),
  triathlon: N("Triathlon"),
  duathlon: N("Duathlon"),
  other: N("Other sport"),
};
const FIELDS = ["kind", "name", "date", "end_date", "sport", "distance_km", "priority", "start_time", "goal_time", "location", "url", "notes"];

function countdown(days) {
  if (days === 0) return t("Today");
  if (days === 1) return t("{n} day to go", { n: number(days) });
  return t("{n} days to go", { n: number(days) });
}

function daysBetween(from, to) {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86400000);
}

function editButton(item, readOnly) {
  if (readOnly) return "";
  return `<button class="btn small" type="button" data-race-edit="${esc(item.id)}" aria-label="${esc(t("Edit {name}", { name: item.name }))}">${esc(t("Edit"))}</button>`;
}

function raceRow(item, today, projected, readOnly) {
  const letter = item.priority ? ` <span class="chip race plan-priority">${esc(t("{letter} race", { letter: item.priority }))}</span>` : "";
  const facts = [longDate(item.date)];
  if (item.sport) facts.push(t(SPORT_LABELS[item.sport] || item.sport));
  if (isNum(item.distance_m)) facts.push(`${number(item.distance_m / 1000, item.distance_m % 1000 ? 1 : 0)} km`);
  const p = projected[`${item.date}|${item.name}`];
  const form = p && isNum(p.tsb)
    ? `<div class="sub">${esc(t("Projected race-day form: {zone} (fitness {ctl}, form {tsb})", { zone: zoneText(p.form_zone), ctl: number(p.ctl), tsb: `${p.tsb > 0 ? "+" : ""}${number(p.tsb)}` }))}</div>`
    : "";
  return `<li data-race-id="${esc(item.id)}"><div class="race-days">${esc(countdown(daysBetween(today, item.date)))}</div>
    <div class="race-what"><b>${esc(item.name)}</b>${letter}<div class="sub">${esc(facts.join(" · "))}</div>${form}</div>
    ${editButton(item, readOnly)}</li>`;
}

function eventRow(item, readOnly) {
  const when = item.end_date
    ? t("{from} to {to}", { from: longDate(item.date), to: longDate(item.end_date) })
    : longDate(item.date);
  return `<li data-race-id="${esc(item.id)}"><div class="race-what"><b>${esc(item.name)}</b>
    <div class="sub">${esc(`${t(KIND_LABELS[item.kind] || KIND_LABELS.other)} · ${when}`)}</div></div>
    ${editButton(item, readOnly)}</li>`;
}

function options(pairs, current) {
  return pairs.map(([value, label]) => `<option value="${esc(value)}"${value === (current ?? "") ? " selected" : ""}>${esc(label)}</option>`).join("");
}

function formHtml(item, kinds, sports) {
  const v = item || { kind: "race" };
  const km = isNum(v.distance_m) ? String(Math.round(v.distance_m / 10) / 100) : "";
  const field = (name, label, input, { race = false, hint = "" } = {}) => `<div class="field"${race ? " data-race-only" : ""}>
      <label for="race-${name}">${esc(label)}</label>${input}${hint ? `<p class="sub" id="race-${name}-hint">${esc(hint)}</p>` : ""}</div>`;
  const kindOptions = options(kinds.map((k) => [k, t(KIND_LABELS[k] || k)]), v.kind);
  const sportOptions = options([["", t("No sport")], ...sports.map((s) => [s, t(SPORT_LABELS[s] || s)])], v.sport || "");
  const priorityOptions = options([["", t("Not set")], ["A", "A"], ["B", "B"], ["C", "C"]], v.priority || "");
  return `<form class="race-form" data-race-form="${esc(v.id || "")}" novalidate aria-labelledby="race-form-title">
    <h3 id="race-form-title">${esc(item ? t("Edit {name}", { name: item.name }) : t("Add a race or event"))}</h3>
    <div class="race-form-grid">
      ${field("kind", t("What"), `<select id="race-kind" name="kind">${kindOptions}</select>`)}
      ${field("name", t("Name"), `<input id="race-name" name="name" required maxlength="200" autocomplete="off" value="${esc(v.name || "")}">`)}
      ${field("date", t("Date"), `<input id="race-date" name="date" type="date" required value="${esc(v.date || "")}">`)}
      ${field("end_date", t("Last day (if more than one day)"), `<input id="race-end_date" name="end_date" type="date" value="${esc(v.end_date || "")}">`)}
      ${field("sport", t("Sport"), `<select id="race-sport" name="sport">${sportOptions}</select>`)}
      ${field("distance_km", t("Distance (km)"), `<input id="race-distance_km" name="distance_km" type="number" inputmode="decimal" min="0.1" max="1000" step="0.01" value="${esc(km)}">`, { race: true })}
      ${field("priority", t("Priority"), `<select id="race-priority" name="priority" aria-describedby="race-priority-hint">${priorityOptions}</select>`, { race: true, hint: t("Your next A race is your main goal.") })}
      ${field("start_time", t("Start time"), `<input id="race-start_time" name="start_time" type="time" value="${esc(v.start_time || "")}">`)}
      ${field("goal_time", t("Goal time"), `<input id="race-goal_time" name="goal_time" inputmode="numeric" placeholder="3:30:00" aria-describedby="race-goal_time-hint" value="${esc(v.goal_time || "")}">`, { race: true, hint: t("Hours, minutes and seconds, for example 3:30:00.") })}
      ${field("location", t("Location"), `<input id="race-location" name="location" maxlength="200" value="${esc(v.location || "")}">`)}
      ${field("url", t("Link"), `<input id="race-url" name="url" type="url" maxlength="500" placeholder="https://" value="${esc(v.url || "")}">`)}
      ${field("notes", t("Notes"), `<textarea id="race-notes" name="notes" rows="2" maxlength="2000">${esc(v.notes || "")}</textarea>`)}
    </div>
    <p class="notice warning" data-race-error role="alert" hidden></p>
    <p class="actions">
      <button class="btn primary" type="submit">${esc(t("Save"))}</button>
      <button class="btn" type="button" data-race-cancel>${esc(t("Cancel"))}</button>
      ${item ? `<button class="btn" type="button" data-race-delete="${esc(item.id)}">${esc(t("Delete"))}</button>` : ""}
    </p>
  </form>`;
}

function cardHtml(data, projected, readOnly) {
  const today = data.today;
  const races = data.items.filter((i) => i.kind === "race");
  const events = data.items.filter((i) => i.kind !== "race");
  const raceList = races.length
    ? `<ul class="plan-races">${races.map((r) => raceRow(r, today, projected, readOnly)).join("")}</ul>`
    : `<p class="empty">${esc(t("No upcoming races."))}</p>`;
  const eventList = events.length
    ? `<h3>${esc(t("Other events"))}</h3><ul class="plan-races plan-events">${events.map((e) => eventRow(e, readOnly)).join("")}</ul>`
    : "";
  const note = races.length && Object.values(projected).some((p) => isNum(p.tsb))
    ? `<p class="sub">${esc(t("Race-day form is a projection from the plan as it stands now."))}</p>`
    : "";
  return `<h2 id="plan-races-title">${esc(t("Races and events"))}</h2>
    ${raceList}${note}${eventList}
    ${readOnly ? "" : `<p class="actions"><button class="btn" type="button" data-race-add aria-expanded="false">${esc(t("Add race or event"))}</button></p>`}
    <div data-race-form-slot></div>
    <p class="sub" data-race-status role="status" aria-live="polite"></p>`;
}

function showRaceFields(form) {
  const isRace = form.elements.kind.value === "race";
  for (const el of form.querySelectorAll("[data-race-only]")) el.hidden = !isRace;
}

function formValues(form) {
  const out = {};
  for (const name of FIELDS) out[name] = (form.elements[name]?.value ?? "").trim();
  if (out.kind !== "race") {
    out.priority = "";
    out.goal_time = "";
  }
  return out;
}

// The browser's own check first (required, dates), in the page's language.
function localProblem(values) {
  if (!values.name) return t("Give it a name.");
  if (!values.date) return t("Choose a date.");
  if (values.kind === "race" && !values.sport) return t("Choose the sport of the race.");
  if (values.end_date && values.end_date < values.date) return t("The last day is before the first day.");
  if (values.goal_time && !/^\d{1,3}:[0-5]\d:[0-5]\d$/.test(values.goal_time)) return t("Hours, minutes and seconds, for example 3:30:00.");
  return "";
}

function serverProblem(error) {
  if (error.code === "duplicate") {
    return t("{name} on {date} is already in your races and events. Change that one instead.", {
      name: error.values?.name || "",
      date: error.values?.date ? longDate(error.values.date) : "",
    });
  }
  if (error.code === "changed_since") return t("This was changed again after that. Undo the later change first.");
  return t(error.message || "Could not save.");
}

async function send(path, body) {
  try {
    return { ok: true, reply: await post(path, body) };
  } catch (error) {
    return { ok: false, error };
  }
}

/** Mount the card into `el` (a <section>). `projection` is the planning page's; a shared
 * page (`readOnly`) shows the list without the form. */
export async function mountRaceEvents(el, { projection = null, stale = () => false, readOnly = false } = {}) {
  const projected = Object.fromEntries((projection?.races || []).map((r) => [`${r.date}|${r.title}`, r]));
  let data;
  let lastFocus = null;

  async function load() {
    const reply = await get("races");
    if (stale()) return false;
    data = reply.data;
    el.innerHTML = cardHtml(data, projected, readOnly);
    return true;
  }

  function status(html) {
    const line = el.querySelector("[data-race-status]");
    if (line) line.innerHTML = html;
  }

  function openForm(item) {
    const slot = el.querySelector("[data-race-form-slot]");
    slot.innerHTML = formHtml(item, data.kinds, data.sports);
    const form = slot.querySelector("form");
    el.querySelector("[data-race-add]")?.setAttribute("aria-expanded", item ? "false" : "true");
    showRaceFields(form);
    form.elements.name.focus();
  }

  function closeForm() {
    el.querySelector("[data-race-form-slot]").innerHTML = "";
    el.querySelector("[data-race-add]")?.setAttribute("aria-expanded", "false");
    (lastFocus && el.contains(lastFocus) ? lastFocus : el.querySelector("[data-race-add]"))?.focus();
  }

  async function reloadAndSay(html, focusId = null) {
    if (!(await load())) return;
    status(html);
    const target = focusId ? el.querySelector(`[data-race-edit="${CSS.escape(focusId)}"]`) : null;
    (target || el.querySelector("[data-race-add]"))?.focus();
  }

  el.addEventListener("click", async (event) => {
    const target = event.target.closest("button");
    if (!target || !el.contains(target)) return;
    if (target.matches("[data-race-add]")) {
      lastFocus = target;
      openForm(null);
    } else if (target.dataset.raceEdit) {
      lastFocus = target;
      openForm(data.items.find((i) => i.id === target.dataset.raceEdit));
    } else if (target.matches("[data-race-cancel]")) {
      closeForm();
    } else if (target.dataset.raceDelete) {
      const item = data.items.find((i) => i.id === target.dataset.raceDelete);
      const done = await send(`races/${encodeURIComponent(target.dataset.raceDelete)}/delete`);
      if (stale()) return;
      if (!done.ok) {
        const box = el.querySelector("[data-race-error]");
        box.textContent = serverProblem(done.error);
        box.hidden = false;
        return;
      }
      await reloadAndSay(`${esc(t("Deleted: {name}.", { name: item?.name || "" }))} <button class="btn small" type="button" data-race-undo="${esc(String(done.reply.data.audit_id))}">${esc(t("Undo"))}</button>`);
    } else if (target.dataset.raceUndo) {
      const done = await send("races/revert", { audit_id: Number(target.dataset.raceUndo) });
      if (stale()) return;
      if (!done.ok) {
        status(esc(serverProblem(done.error)));
        return;
      }
      const back = done.reply.data.race;
      await reloadAndSay(esc(t("{name} is back.", { name: back?.name || "" })), back?.id);
    }
  });

  el.addEventListener("change", (event) => {
    const form = event.target.closest("form[data-race-form]");
    if (form && event.target.name === "kind") showRaceFields(form);
  });

  el.addEventListener("submit", async (event) => {
    const form = event.target.closest("form[data-race-form]");
    if (!form) return;
    event.preventDefault();
    const box = form.querySelector("[data-race-error]");
    const values = formValues(form);
    const problem = localProblem(values);
    if (problem) {
      box.textContent = problem;
      box.hidden = false;
      return;
    }
    const id = form.dataset.raceForm;
    const done = await send(id ? `races/${encodeURIComponent(id)}` : "races", values);
    if (stale()) return;
    if (!done.ok) {
      box.textContent = serverProblem(done.error);
      box.hidden = false;
      return;
    }
    const saved = done.reply.data.race;
    await reloadAndSay(esc(t("Saved: {name}.", { name: saved?.name || "" })), saved?.id);
  });

  await load();
}
