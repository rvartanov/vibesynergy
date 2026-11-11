// The weather of one coming day: a line with the summary, temperature, wind and rain, and
// where it is for. The calendar and the day page of a coming day both show it.
import { esc, isNum, number } from "./format.js";
import { t } from "./i18n.js";
import { temperatureRange, windText } from "./weather_units.js";

// Where the forecast is for: the place chosen in Settings, or where you last trained outside.
export function forecastPlace(location) {
  if (location?.kind === "chosen" && location.name) return location.name.split(",")[0];
  if (location?.kind === "last_outdoor") return t("where you last trained outside");
  return "";
}

export function weatherHtml(w, location) {
  if (!w) return "";
  // A forecast: only for today and the days after it.
  const temp = isNum(w.temp_min_c) && isNum(w.temp_max_c) ? temperatureRange(w.temp_min_c, w.temp_max_c) : "";
  const wind = isNum(w.wind_ms) ? t("wind {speed}", { speed: windText(w.wind_ms) }) : "";
  const rain = w.rain_mm ? t("{n} mm rain", { n: number(w.rain_mm, 1) }) : "";
  const flags = (w.flags || []).map((f) => `<span class="chip warning">${esc(t(f))}</span>`).join(" ");
  const text = [w.summary ? t(w.summary) : "", temp, wind, rain].filter(Boolean).join(" · ");
  const where = forecastPlace(location);
  const label = where ? t("Weather in {place}", { place: where }) : t("Weather");
  return text ? `<p class="sched-weather"><span class="sub">${esc(label)}</span> ${esc(text)} ${flags}</p>` : "";
}
