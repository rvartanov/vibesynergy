// Keep a sync failure actionable; raw diagnostics belong in a closed disclosure.
import { esc } from "./format.js";
import { t } from "./i18n.js";

const LOGIN = /log ?in|sign.?in|token|unauthori[sz]ed|\b401\b|\b403\b|credentials|password|expired|reauth/i;
const NETWORK = /network|connect|timeout|timed out|unreachable|temporar|\b50[0234]\b/i;

export function syncAdvice(source, error) {
  const calendar = source === "caldav";
  if (calendar) {
    if (LOGIN.test(error || "")) return t("Your calendar couldn't be read. Sign in again in Settings → Sources → Calendar.");
    if (NETWORK.test(error || "")) return t("Your calendar couldn't be read. Try again later.");
    return t("Your calendar couldn't be read. Check Settings → Sources → Calendar or try again later.");
  }
  if (LOGIN.test(error || "")) return t("Your data couldn't be updated. Sign in again in Settings → Sources.");
  if (NETWORK.test(error || "")) return t("Your data couldn't be updated. Try again later.");
  return t("Your data couldn't be updated. Check Settings → Sources or try again later.");
}

export function syncDetails(error) {
  return error
    ? `<details><summary>${t("Technical details")}</summary><span class="sub">${esc(error)}</span></details>`
    : "";
}
