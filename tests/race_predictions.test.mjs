import assert from "node:assert/strict";
import test from "node:test";

import { predictionsHtml } from "../src/vibesynergy/portal/static/js/race_predictions.js";

test("a table per distance, with certainty, change and Garmin", () => {
  const html = predictionsHtml({
    predictions: [
      { key: "10k", seconds: 3000, pace_s_per_km: 300, certainty: "high", change_s: -40, garmin: { seconds: 2970 }, from: { kind: "threshold", distance_m: 12000, date: "2026-03-01" } },
      { key: "50k", seconds: 16800, pace_s_per_km: 336, certainty: "medium", change_s: null, garmin: null, from: { kind: "threshold", distance_m: 12000, date: "2026-03-01" } },
    ],
  });
  assert.match(html, /50:00/);
  assert.match(html, /0:40 faster/);
  assert.match(html, /49:30/);
  assert.match(html, /data-certainty="medium"/);
  assert.match(html, /threshold pace \(5:00 per km/);
});

test("without evidence it says what to do", () => {
  assert.match(predictionsHtml({ predictions: [{ key: "5k", seconds: null }] }), /set your threshold pace/);
});

test("a race as the basis links to it, and races are scored against the prediction", () => {
  const html = predictionsHtml({
    predictions: [
      { key: "10k", seconds: 2700, pace_s_per_km: 270, certainty: "high", change_s: null, garmin: null, from: { kind: "race", distance_m: 10000, date: "2026-03-01", activity: "garmin:1" } },
    ],
    races: [{ key: "10k", date: "2026-03-01", activity: "garmin:1", predicted_s: 2760, actual_s: 2700, error_s: -60 }],
  });
  assert.match(html, /From your race over 10.0 km/);
  assert.match(html, /href="#\/activity\/garmin%3A1"/);
  assert.match(html, /1:00 faster than predicted/);
});
