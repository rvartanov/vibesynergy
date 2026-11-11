// Weather in the units you chose (Settings → System: temperature °C/°F, wind km/h, m/s,
// knots or Beaufort). Values arrive in °C and m/s; the server has the same rules in
// core/weather_units.py for what it writes itself.
const BEAUFORT_MS = [0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7];

let units = { temperature: "c", wind: "kmh" };

export function setWeatherUnits(next) {
  if (next && typeof next === "object") units = { ...units, ...next };
}

export function beaufort(ms) {
  const force = BEAUFORT_MS.findIndex((limit) => ms < limit);
  return force === -1 ? 12 : force;
}

export function degrees(celsius, scale = units.temperature) {
  return Math.round(scale === "f" ? (celsius * 9) / 5 + 32 : celsius);
}

export function temperatureRange(low, high, scale = units.temperature) {
  return `${degrees(low, scale)}–${degrees(high, scale)} ${scale === "f" ? "°F" : "°C"}`;
}

export function windText(ms, unit = units.wind) {
  if (unit === "ms") return `${Math.round(ms)} m/s`;
  if (unit === "kn") return `${Math.round((ms * 3600) / 1852)} kn`;
  if (unit === "bft") return `${beaufort(ms)} Bft`;
  return `${Math.round(ms * 3.6)} km/h`;
}
