// Training → Zones & thresholds → About you (from the former Settings → You tab): sex,
// date of birth (the coach gets the age only), maximum and resting heart rate for
// estimating training load. Each change saves at once to
// the config; the service reads it at the next restart, which the card offers.
import { get, post } from "./api.js";
import { esc } from "./format.js";
import { N, t } from "./i18n.js";

const LIMITS = { max_hr: [100, 250], resting_hr: [25, 120] };
const ERRORS = [N("Enter a heart rate between {min} and {max}.")];

function option(value, label, current) {
  return `<option value="${esc(value)}"${value === current ? " selected" : ""}>${esc(label)}</option>`;
}

function waiting(s) {
  return Array.isArray(s.restart_tabs) && s.restart_tabs.includes("training");
}

export async function athleteProfile(el, { stale }) {
  let s = (await get("settings")).data;
  if (stale()) return;
  const u = s.user || {};
  el.innerHTML = `<h2 id="athlete-profile-title">${esc(t("About you"))}</h2>
    <p class="sub">${esc(t("Used to estimate training load from heart rate. Leave the heart rates empty to use your zones and typical resting heart rate."))}</p>
    <div class="settings-grid">
      <div class="settings-field"><label for="athlete-sex">${esc(t("Sex"))}</label>
        <select class="btn" id="athlete-sex" name="sex">${option("", t("Not set"), u.sex || "")}${option("male", t("Male"), u.sex || "")}${option("female", t("Female"), u.sex || "")}</select></div>
      <div class="settings-field"><label for="athlete-birth-date">${esc(t("Date of birth"))}</label>
        <input class="btn" type="date" id="athlete-birth-date" name="birth_date" min="1900-01-01" max="${esc(new Date().toISOString().slice(0, 10))}" value="${esc(u.birth_date ?? "")}" aria-describedby="athlete-birth-hint">
        <p class="sub" id="athlete-birth-hint">${esc(t("Your coach only sees your age."))}</p></div>
      <div class="settings-field"><label for="athlete-max-hr">${esc(t("Max heart rate"))}</label>
        <input class="btn" type="number" id="athlete-max-hr" name="max_hr" min="100" max="250" step="1" inputmode="numeric" value="${esc(u.max_hr ?? "")}" placeholder="${esc(t("from your zones"))}"></div>
      <div class="settings-field"><label for="athlete-resting-hr">${esc(t("Resting heart rate"))}</label>
        <input class="btn" type="number" id="athlete-resting-hr" name="resting_hr" min="25" max="120" step="1" inputmode="numeric" value="${esc(u.resting_hr ?? "")}" placeholder="${esc(t("from your data"))}"></div>
    </div>
    <p class="sub" data-athlete-status role="status" aria-live="polite"></p>
    <div data-athlete-restart></div>`;
  el.setAttribute("aria-labelledby", "athlete-profile-title");
  const status = el.querySelector("[data-athlete-status]");
  const restartBox = el.querySelector("[data-athlete-restart]");

  const say = (text, error = false) => {
    status.textContent = text;
    status.classList.toggle("error-text", error);
  };
  const showRestart = () => {
    restartBox.innerHTML = waiting(s)
      ? `<div class="notice warning">${esc(t("Saved settings apply after the service restarts."))}
        ${s.can_restart ? ` <button class="btn primary" type="button" data-athlete-restart-now>${esc(t("Restart now"))}</button>` : ` ${esc(t("Restart the service on this server to apply them."))}`}</div>`
      : "";
  };
  showRestart();

  el.addEventListener("change", async (event) => {
    const input = event.target.closest("[name]");
    if (!input) return;
    const values = {};
    for (const name of ["max_hr", "resting_hr"]) {
      const field = el.querySelector(`[name=${name}]`);
      const raw = field.value.trim();
      const [min, max] = LIMITS[name];
      const value = raw === "" ? null : Number(raw);
      if (value !== null && !(Number.isFinite(value) && value >= min && value <= max)) {
        field.setAttribute("aria-invalid", "true");
        say(t(ERRORS[0], { min, max }), true);
        if (field === input) field.focus();
        return;
      }
      field.removeAttribute("aria-invalid");
      values[name] = value;
    }
    values.sex = el.querySelector("[name=sex]").value || null;
    values.birth_date = el.querySelector("[name=birth_date]").value || null;
    say(t("Saving…"));
    try {
      s = (await post("settings", { changes: { user: values } })).data;
    } catch (error) {
      say(t("Not saved: {error}", { error: error.message }), true);
      return;
    }
    if (stale()) return;
    say(t("Saved."));
    showRestart();
  });

  el.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-athlete-restart-now]");
    if (!button) return;
    button.disabled = true;
    button.textContent = t("Restarting…");
    try {
      await post("settings/restart");
    } catch (error) {
      say(t("Not saved: {error}", { error: error.message }), true);
      button.disabled = false;
      return;
    }
    for (let i = 0; i < 30; i++) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      try {
        if (!(await get("settings")).data.restart_needed) {
          location.reload();
          return;
        }
      } catch {
        // still restarting
      }
    }
    button.textContent = t("Still restarting; reload the page in a moment");
  });
}
