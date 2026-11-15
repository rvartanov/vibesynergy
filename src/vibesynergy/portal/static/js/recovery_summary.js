// The start page's recovery summary line.
import { number } from "./format.js";
import { t } from "./i18n.js";

// The summary follows today's advice (the verdict): one sign off that the advice does not act
// on (resting heart rate alone) is worth watching, not a reason to ease off (#37).
export function recoverySummaryText(off, total, level) {
  if (!off) return t("You look recovered as usual.");
  const vars = { n: number(off), total: number(total) };
  const signs = off === 1 ? t("1 of {total} signs is off your usual.", vars) : t("{n} of {total} signs are off your usual.", vars);
  const easier = level === "amber" || level === "red";
  return `${signs} ${easier ? t("Take it a little easier.") : t("Keep an eye on it; on its own it doesn't change today's advice.")}`;
}
