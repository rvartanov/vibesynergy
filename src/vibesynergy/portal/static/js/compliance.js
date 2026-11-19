// Compliance of a done planned workout: a percentage with a tone and icon, so it
// never rests on colour alone. The time done against the time planned in VibeSynergy's plan.
import { esc, isNum, number } from "./format.js";
import { t } from "./i18n.js";

export function complianceTone(pct) {
  if (pct >= 80 && pct <= 120) return "success";
  if (pct >= 50 && pct <= 150) return "warning";
  return "danger";
}

export function complianceChip(pct) {
  if (!isNum(pct)) return "";
  const how = t("Time done against time planned");
  return `<span class="chip ${complianceTone(pct)}" title="${esc(how)}">${esc(t("{pct}% as planned", { pct: number(pct) }))}</span>`;
}
