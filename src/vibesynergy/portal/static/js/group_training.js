// Today → "Group training from RunSynergy": a "Training geven" session whose workout can be
// the group schedule from RunSynergy (warm-up and cool-down until lap, the core on time so
// you can blow the whistle, Z1 for yourself). Nothing changes until "Adopt" is chosen; then
// the plan's normal delivery puts it on the watch. The same proposal shows under Waiting
// for you and on Planning, always with its day, a mark when that has passed and the plan's
// other sessions that day (#113).
import { post } from "./api.js";
import { esc, shortDay } from "./format.js";
import { N, t } from "./i18n.js";

const PARTS = { warmup: N("Warm-up"), work: N("Core"), cooldown: N("Cool-down") };

function stepText(step) {
  const min = Math.round((step.duration_s || 0) / 60);
  const length = step.until_lap ? t("until lap, about {min} min", { min }) : t("{min} min", { min });
  return step.cue ? `${step.cue} (${length})` : length;
}

// The workout as a short list: warm-up and cool-down until lap, the core with its repeats.
function stepsHtml(steps) {
  const rows = [];
  for (const step of steps || []) {
    const kind = step.kind === "repeat" ? step.steps?.[0]?.kind || "work" : step.kind;
    const text = step.kind === "repeat"
      ? `${step.times}× ${(step.steps || []).map(stepText).join(" · ")}`
      : stepText(step);
    rows.push(`<li><strong>${esc(t(PARTS[kind] || PARTS.work))}</strong>: ${esc(text)}</li>`);
  }
  return `<ol class="workout-steps">${rows.join("")}</ol>`;
}

/** The day it is for, with the start time when there is one: "do 20 nov, 19:30". */
export function groupWhen(p) {
  return [shortDay(p.date), p.start].filter(Boolean).join(", ");
}

/** What to weigh before adopting: the day has passed, or the plan already has a session. */
export function groupMarksHtml(p) {
  const marks = [];
  if (p.past) marks.push(`<p class="sub"><span class="chip warning">${esc(t("Passed"))}</span> ${esc(t("This day has passed."))}</p>`);
  const others = (p.same_day || []).map((s) => s.name).filter(Boolean);
  if (others.length) marks.push(`<p class="sub"><span class="chip info">${esc(t("Same day"))}</span> ${esc(t("That day your plan also has {names}.", { names: others.join(", ") }))}</p>`);
  return marks.join("");
}

export function groupTrainingHtml(p) {
  const name = [p.club, p.title].filter(Boolean).join(": ");
  const head = `<p><strong>${esc(name || t("Group training"))}</strong> · <time datetime="${esc(p.date)}">${esc(groupWhen(p))}</time></p>${groupMarksHtml(p)}`;
  if (p.state === "cancelled") {
    return `<div class="notice" data-group-training="${esc(p.session_id)}">${head}
      <p>${esc(t("RunSynergy cancelled this group training. The session stays in your plan until you change it."))}</p>
    </div>`;
  }
  const read = p.core_read === "fallback"
    ? `<p class="sub">${esc(t("The core could not be read as steps; it is one timed block with the schedule's text."))}</p>`
    : "";
  return `<div class="notice" data-group-training="${esc(p.session_id)}">${head}
    <p>${esc(t("A workout from the group schedule for your Training geven session: {minutes} min, with the core on time so you can blow the whistle. You run along in Z1.", { minutes: p.minutes }))}</p>
    ${read}
    <details><summary>${esc(t("Show the workout"))}</summary>${stepsHtml(p.steps)}</details>
    <p><button class="btn primary" type="button" data-adopt-group="${esc(p.session_id)}">${esc(t("Adopt"))}</button></p>
    <p class="sub" role="status" data-group-status></p>
  </div>`;
}

export function groupTrainingsHtml(list) {
  if (!Array.isArray(list) || !list.length) return "";
  return `<div class="dash-group-trainings" aria-labelledby="dash-group-title">
    <h3 id="dash-group-title">${esc(t("Group training from RunSynergy"))}</h3>
    ${list.map(groupTrainingHtml).join("")}
  </div>`;
}

export function bindGroupTrainings(view) {
  view.querySelectorAll("[data-adopt-group]").forEach((button) => {
    button.addEventListener("click", async () => {
      const box = button.closest("[data-group-training]");
      const status = box?.querySelector("[data-group-status]");
      button.disabled = true;
      if (status) status.textContent = t("Saving…");
      try {
        await post("plan/session/group-training", { session_id: button.dataset.adoptGroup });
        button.remove();
        if (status) status.textContent = t("Adopted. It reaches your watch within a few minutes.");
      } catch (error) {
        button.disabled = false;
        if (status) status.textContent = t(error.message);
      }
    });
  });
}
