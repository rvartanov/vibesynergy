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
