import assert from "node:assert/strict";
import test from "node:test";

import { markCardHtml, purposeText, thresholdOutcome } from "../src/vibesynergy/portal/static/js/purpose_mark.js";

test("an automatic mark says so and how to undo it", () => {
  assert.match(purposeText("test", { automatic: true }), /Marked as a test by itself/);
  assert.match(purposeText("race", { automatic: true }), /Marked as a race by itself/);
  assert.match(purposeText(null, { undone: true }), /won't be marked again/);
  assert.match(purposeText("test", { automatic: false }), /Saved purpose: Test/);
});

test("after saving: new proposals, withdrawn ones, or why there is none", () => {
  assert.match(thresholdOutcome({ values: [{}], proposed: [7] }), /wait for you/);
  assert.match(thresholdOutcome({ withdrawn: [7] }), /withdrawn/);
  assert.equal(
    thresholdOutcome({ values: [], missing: [{ metric: "ftp", reason: "no_power" }], proposed: [] }),
    "No new threshold: there is no power data.",
  );
  assert.equal(thresholdOutcome(undefined), "");
});

test("the card lists what the test gave", () => {
  const html = markCardHtml({
    purpose: "test",
    threshold_values: [
      { metric: "css", value: 110, unit: "s/100m", status: "proposed" },
      { metric: "ftp", value: 238, unit: "W", status: "current" },
    ],
  });
  assert.match(html, /Thresholds from this test/);
  assert.match(html, /1:50 \/100 m/);
  assert.match(html, /238 W/);
  assert.match(html, /waiting for you/);
  assert.match(html, /href="#\/zones"/);
  assert.equal(markCardHtml({ threshold_values: [] }), "");
});
