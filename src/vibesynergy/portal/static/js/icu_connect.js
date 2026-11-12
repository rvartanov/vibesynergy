// Settings → Sources → intervals.icu: the API key and athlete id, entered here and
// stored encrypted on the server. The key field is write-only: the page only learns whether
// a key is set. An empty key field keeps the stored key (to change only the athlete id).
import { post } from "./api.js";
import { esc } from "./format.js";
import { N, t } from "./i18n.js";

export function icuConnectHtml(src) {
  const login = src.login || {};
  const connected = login.state === "logged in";
  const legacy = login.origin === "environment";
  const note = legacy
    ? t("VibeSynergy still reads this key from the server's settings file. Save it here once to keep it safe in VibeSynergy.")
    : connected
      ? t("A key is saved. Leave the key field empty to keep it.")
      : t("Find both on intervals.icu under Settings → Developer settings.");
  // Like Garmin's "Sign in again": the form waits behind a disclosure, open only when the
  // key still comes from the server's settings file and needs one save here.
  const summary = connected ? t("Change the intervals.icu key") : t("Connect intervals.icu");
  return `<div data-source-connect data-icu-connect data-autosave-skip>
    <details${legacy ? " open" : ""}><summary>${esc(summary)}</summary>
    <div class="settings-grid">
      <label>${t("Athlete id")} <input class="btn" name="icu_athlete" autocomplete="off" spellcheck="false" maxlength="40" value="${esc(login.account || "")}" aria-describedby="icu-key-note"></label>
      <label>${t("API key")} <input class="btn" name="icu_key" type="password" autocomplete="off" maxlength="200" placeholder="${esc(connected ? t("saved") : "")}" aria-describedby="icu-key-note"></label>
    </div>
    <p class="sub" id="icu-key-note">${esc(note)}</p>
    <p><button class="btn${connected && !legacy ? "" : " primary"}" type="button" data-save-icu>${t(connected && !legacy ? N("Save intervals.icu") : N("Connect intervals.icu"))}</button></p>
    </details>
    ${connected && !legacy ? `<p><button class="btn" type="button" data-disconnect="intervals_icu">${t("Disconnect intervals.icu")}</button></p>` : ""}
    <p class="sub" role="status" data-connect-status></p>
  </div>`;
}

export function bindIcuConnect(view) {
  const box = view.querySelector("[data-icu-connect]");
  if (!box) return;
  const button = box.querySelector("[data-save-icu]");
  const status = box.querySelector("[data-connect-status]");
  const key = box.querySelector("[name=icu_key]");
  const athlete = box.querySelector("[name=icu_athlete]");
  button.addEventListener("click", async () => {
    for (const field of [key, athlete]) field.removeAttribute("aria-invalid");
    if (!athlete.value.trim()) {
      athlete.setAttribute("aria-invalid", "true");
      athlete.focus();
      status.textContent = t("Enter your intervals.icu athlete id, such as i12345.");
      return;
    }
    button.disabled = true;
    try {
      const { data } = await post("sources/intervals_icu/credentials", {
        api_key: key.value,
        athlete_id: athlete.value.trim(),
      });
      key.value = "";
      key.placeholder = t("saved");
      status.textContent = t("intervals.icu connected as {name}.", { name: data.athlete_id });
    } catch (error) {
      status.textContent = t(error.message);
      const field = /API key/.test(error.message) ? key : athlete;
      field.setAttribute("aria-invalid", "true");
      field.focus();
    } finally {
      button.disabled = false;
    }
  });
}
