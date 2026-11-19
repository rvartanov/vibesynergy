import assert from "node:assert/strict";
import test from "node:test";

import { disagreementLines, secondOpinionsHtml } from "../src/vibesynergy/portal/static/js/daily_advice.js";

const OPINION = {
  id: 7,
  at: "2026-11-19T07:10:00+00:00",
  author_name: "Second coach",
  advice: "Skip the ride today.",
  colour: "amber",
  safety_level: "flag",
  differences: [
    { field: "safety_level", primary: "ease", advisory: "flag" },
    { field: "sessions", changes: [{ title: "duurrit Z2", primary: "keep", advisory: "skip" }] },
  ],
  disagrees: true,
};

test("a difference with the primary AI coach is said per level and per session", () => {
  assert.deepEqual(disagreementLines(OPINION.differences), [
    "Safety level: your AI coach “take it a bit easier”, second opinion “no hard training”.",
    "Duurrit Z2: your AI coach “keep”, second opinion “skip”.",
  ]);
  assert.deepEqual(disagreementLines([]), []);
});

test("a second opinion that differs is listed, flagged and open", () => {
  const html = secondOpinionsHtml({ second_opinions: [OPINION] });
  assert.match(html, /data-second-opinions open/);
  assert.match(html, /Second opinion \(1\)/);
  assert.match(html, /data-second-opinion-differs/);
  assert.match(html, /Second coach, advisory AI coach/);
  assert.match(html, /Skip the ride today\./);
  assert.match(html, /has the last word/);
});

test("a second opinion that agrees is listed without a flag, closed", () => {
  const html = secondOpinionsHtml({ second_opinions: [{ ...OPINION, differences: [], disagrees: false }] });
  assert.doesNotMatch(html, /data-second-opinion-differs/);
  assert.doesNotMatch(html, / open>/);
  assert.match(html, /Agrees with your AI coach/);
});

test("no second opinion, nothing shown", () => {
  assert.equal(secondOpinionsHtml({ second_opinions: [] }), "");
  assert.equal(secondOpinionsHtml({}), "");
});
