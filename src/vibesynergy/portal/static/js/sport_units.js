// Mirrors query/units.py. Source measurements stay in SI; this module owns display units.
import { clock, isNum, km, number } from "./format.js";
import { t } from "./i18n.js";

export function sportFamily(sport) {
  const key = (sport || "").toLowerCase();
  if (key.includes("swim")) return key.includes("pool") || key.includes("lap") ? "pool_swim" : "open_water_swim";
  if (/run|walk|hik/.test(key)) return "run";
  if (/cycl|bik|ride/.test(key)) return "cycle";
  if (key.includes("row")) return "row";
  if (/strength|yoga|pilates|breath|fitness|training|meditat|cardio|hiit|elliptical/.test(key)) return "time";
  return "other";
}

export function speedUnit(sport) {
  const family = sportFamily(sport);
  if (family === "run") return "/km";
  if (family === "pool_swim" || family === "open_water_swim") return "/100m";
  if (family === "row") return "/500m";
  if (family === "cycle") return "km/h";
  return null;
}

export function distanceText(sport, metres) {
  if (!isNum(metres)) return "–";
  return ["pool_swim", "open_water_swim", "row"].includes(sportFamily(sport))
    ? `${number(metres)} m` : km(metres);
}

export function speedText(sport, speed) {
  const unit = speedUnit(sport);
  if (!unit || !isNum(speed) || speed <= 0) return "–";
  if (unit === "km/h") return `${number(speed * 3.6, 1)} km/h`;
  const metres = unit === "/km" ? 1000 : unit === "/500m" ? 500 : 100;
  return `${clock(metres / speed)}${unit}`;
}

// An activity's average speed (m/s): the recorded one, else distance over moving (or
// elapsed) time, as the server derives it for imported GPX and TCX files.
export function averageSpeed(activity) {
  if (isNum(activity?.avg_speed) && activity.avg_speed > 0) return activity.avg_speed;
  const seconds = activity?.moving_s || activity?.elapsed_s;
  return isNum(activity?.distance_m) && activity.distance_m > 0 && isNum(seconds) && seconds > 0 ? activity.distance_m / seconds : null;
}

export function speedChannel(sport) {
  const unit = speedUnit(sport);
  if (!unit) return null;
  if (unit === "km/h") return ["speed", t("Speed"), unit, (v) => v * 3.6, (v) => number(v, 1)];
  const metres = unit === "/km" ? 1000 : unit === "/500m" ? 500 : 100;
  return ["speed", t("Pace"), unit, (v) => v > 0 ? metres / v / 60 : null, (v) => clock(v * 60)];
}

export function speedLabel(sport, average = false) {
  const unit = speedUnit(sport);
  if (!unit) return null;
  return unit === "km/h" ? t(average ? "Average speed" : "Speed") : t(average ? "Average pace" : "Pace");
}
