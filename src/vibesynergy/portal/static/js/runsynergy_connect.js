// Settings → Sources → RunSynergy: the personal calendar link from RunSynergy (Mijn gegevens →
// Agendalink). The link is a secret: it is stored encrypted on the server and the page only
// learns whether one is set and its host. Saving switches the source on and reads it once.
import { post } from "./api.js";
import { esc } from "./format.js";
import { t } from "./i18n.js";

export function runsynergyConnectHtml(src) {
  const login = src.login || {};
  const connected = login.state === "logged in";
  const summary = connected ? t("Change the RunSynergy calendar link") : t("Connect RunSynergy");
  return `<div data-source-connect data-runsynergy-connect data-autosave-skip>
    <p class="sub">${esc(t("The group trainings you give come from RunSynergy. Each one becomes a workout proposal for your Training geven session, with the times of the group schedule."))}</p>
    <details${connected ? "" : " open"}><summary>${esc(summary)}</summary>
    <label>${t("Calendar link")} <input class="btn" name="runsynergy_url" type="password" autocomplete="off" spellcheck="false" maxlength="600" placeholder="${esc(connected ? t("saved") : "https://…/agenda/….ics")}" aria-describedby="runsynergy-note"></label>
    <p class="sub" id="runsynergy-note">${esc(t("Find it in RunSynergy under My details → Calendar link. Anyone with the link can read your schedule, so it is kept encrypted."))}</p>
    <p><button class="btn primary" type="button" data-save-runsynergy>${t("Save link")}</button></p>
    </details>
    ${connected ? `<p><button class="btn" type="button" data-disconnect="runsynergy">${t("Disconnect RunSynergy")}</button></p>` : ""}
    <p class="sub" role="status" data-connect-status></p>
  </div>`;
}

export function bindRunsynergyConnect(view) {
  const box = view.querySelector("[data-runsynergy-connect]");
  if (!box) return;
  const button = box.querySelector("[data-save-runsynergy]");
  const status = box.querySelector("[data-connect-status]");
  const field = box.querySelector("[name=runsynergy_url]");
  button.addEventListener("click", async () => {
    field.removeAttribute("aria-invalid");
    if (!field.value.trim()) {
      field.setAttribute("aria-invalid", "true");
      field.focus();
      status.textContent = t("Paste your RunSynergy calendar link.");
      return;
    }
    button.disabled = true;
    try {
      const { data } = await post("sources/runsynergy/link", { url: field.value.trim() });
      field.value = "";
      field.placeholder = t("saved");
      status.textContent = t("RunSynergy connected ({host}). The group trainings come in within a few minutes.", { host: data.host });
    } catch (error) {
      status.textContent = t(error.message);
      field.setAttribute("aria-invalid", "true");
      field.focus();
    } finally {
      button.disabled = false;
    }
  });
}
