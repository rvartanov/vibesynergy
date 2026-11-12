import assert from "node:assert/strict";
import test from "node:test";

import { predictionsHtml } from "../src/vibesynergy/portal/static/js/race_predictions.js";

test("a table per distance, with certainty, change and Garmin", () => {
  const html = predictionsHtml({
    predictions: [
      { key: "10k", seconds: 3072, pace_s_per_km: 307.2, certainty: "high", change_s: -40, garmin: { seconds: 3039 }, from: { kind: "threshold", distance_m: 11613, date: "2026-11-03" } },
      { key: "50k", seconds: 17213, pace_s_per_km: 344.3, certainty: "medium", change_s: null, garmin: null, from: { kind: "threshold", distance_m: 11613, date: "2026-11-03" } },
    ],
  });
  assert.match(html, /51:12/);
  assert.match(html, /0:40 faster/);
  assert.match(html, /50:39/);
  assert.match(html, /data-certainty="medium"/);
  assert.match(html, /threshold pace \(5:10 per km/);
});

test("without evidence it says what to do", () => {
  assert.match(predictionsHtml({ predictions: [{ key: "5k", seconds: null }] }), /set your threshold pace/);
});
