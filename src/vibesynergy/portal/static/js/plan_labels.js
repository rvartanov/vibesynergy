// One vocabulary for calendar context; no inference of exercise from an appointment.
import { t } from "./i18n.js";

export function planKind(item) {
  if (item.kind === "commitment") {
    if (item.extra?.classification === "exercise") return t("Planned personal exercise");
    if (item.extra?.classification === "coaching") return t("Coaching commitment");
    return t("Personal commitment");
  }
  if (item.kind === "mirror") {
    const name = item.extra?.calendar_name;
    return name ? t("Calendar: {name}", { name }) : t("Connected calendar");
  }
  if (item.kind === "event") return t("Event");
  return t("Planned workout");
}

// Garmin workouts and events the plan does not have: say where they are, why they are apart
// and what to do about it. ``rows`` is the list's <li> markup. The plan always lives in
// VibeSynergy (#111); ``_planSource`` stays for the callers.
export function outsidePlanHtml(_planSource, rows) {
  if (!rows) return "";
  const why = t("Your plan is in VibeSynergy. The workouts below are on your Garmin calendar but not in your plan, so they do not count in planned versus done. If one belongs to your plan, ask your AI coach to add it.");
  return `<h3>${t("Only on your Garmin calendar")}</h3><p class="sub">${why}</p><ul class="trends">${rows}</ul>`;
}
