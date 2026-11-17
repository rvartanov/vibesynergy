// Today → "Group training from RunSynergy": a "Training geven" session whose workout can be
// the group schedule from RunSynergy (warm-up and cool-down until lap, the core on time so
// you can blow the whistle, Z1 for yourself). Nothing changes until "Adopt" is chosen; then
// the plan's normal delivery puts it on the watch.
import { post } from "./api.js";
import { esc, longDate } from "./format.js";
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

function oneHtml(p) {
  const when = [longDate(p.date), p.start].filter(Boolean).join(", ");
  const name = [p.club, p.title].filter(Boolean).join(": ");
  const head = `<p><strong>${esc(name || t("Group training"))}</strong> · ${esc(when)}</p>`;
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
    ${list.map(oneHtml).join("")}
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
