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

// Garmin workouts the plan does not have: say where they are, why they are apart and what
// to do about it. ``rows`` is the list's <li> markup; ``planSource`` is "vibesynergy" when
// the plan lives in VibeSynergy, else it comes from intervals.icu.
export function outsidePlanHtml(planSource, rows) {
  if (!rows) return "";
  const why = planSource === "vibesynergy"
    ? t("Your plan is in VibeSynergy. The workouts below are on your Garmin calendar but not in your plan, so they do not count in planned versus done. If one belongs to your plan, ask your coach to add it.")
    : t("Your plan comes from intervals.icu. The workouts below are on your Garmin calendar but not in intervals.icu, so they do not count in planned versus done. If one belongs to your plan, add it in intervals.icu.");
  return `<h3>${t("Only on your Garmin calendar")}</h3><p class="sub">${why}</p><ul class="trends">${rows}</ul>`;
}
