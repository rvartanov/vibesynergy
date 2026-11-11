import assert from "node:assert/strict";
import test from "node:test";

import { beaufort, setWeatherUnits, temperatureRange, windText } from "../src/vibesynergy/portal/static/js/weather_units.js";

test("temperature in °C by default and °F when chosen", () => {
  assert.equal(temperatureRange(3, 9), "3–9 °C");
  setWeatherUnits({ temperature: "f" });
  assert.equal(temperatureRange(3, 9), "37–48 °F");
  setWeatherUnits({ temperature: "c" });
});

test("wind in km/h, m/s, knots or Beaufort", () => {
  assert.equal(windText(5), "18 km/h");
  assert.equal(windText(5, "ms"), "5 m/s");
  assert.equal(windText(5, "kn"), "10 kn");
  assert.equal(windText(5, "bft"), "3 Bft");
  setWeatherUnits({ wind: "bft" });
  assert.equal(windText(17.2), "8 Bft");
  setWeatherUnits({ wind: "kmh" });
});

test("Beaufort matches the server's bounds", () => {
  assert.deepEqual([0, 0.29, 0.3, 7.9, 8, 32.6, 32.7, 50].map(beaufort), [0, 0, 1, 4, 5, 11, 12, 12]);
});
