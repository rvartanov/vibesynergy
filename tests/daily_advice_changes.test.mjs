import assert from "node:assert/strict";
import test from "node:test";

import { sessionChangeText, versionDiffText } from "../src/vibesynergy/portal/static/js/daily_advice.js";

test("a changed session is named as written, with a capital, and says what changed", () => {
  assert.equal(
    sessionChangeText({ title: "rustige rit Z2 1u", from: "keep", to: "skip" }),
    "Rustige rit Z2 1u: dropped",
  );
  assert.equal(sessionChangeText({ title: "Duurloop", from: "keep", to: "adjust" }), "Duurloop: adjusted");
  assert.equal(sessionChangeText({ title: "Duurloop", from: null, to: "keep" }), "Duurloop: added");
  assert.equal(sessionChangeText({ title: "Duurloop", from: "keep", to: null }), "Duurloop: removed");
});

test("the Changed line lists every session and keeps the colour change", () => {
  const text = versionDiffText([
    { field: "colour", from: "amber", to: "green" },
    {
      field: "sessions",
      sessions: ["Rustige rit Z2 1u", "Training geven"],
      changes: [
        { title: "Rustige rit Z2 1u", from: "keep", to: "skip" },
        { title: "Training geven", from: "keep", to: "adjust" },
      ],
    },
  ]);
  assert.equal(text, "colour amber → green; Rustige rit Z2 1u: dropped; Training geven: adjusted");
});

test("an older record without per-session changes still reads as titles", () => {
  assert.equal(
    versionDiffText([{ field: "sessions", sessions: ["duurloop"] }]),
    "Duurloop: changed",
  );
});
