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
