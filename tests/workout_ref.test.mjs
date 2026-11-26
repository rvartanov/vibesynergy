import assert from "node:assert/strict";
import test from "node:test";

import { workoutRefHtml } from "../src/vibesynergy/portal/static/js/workout_ref.js";

test("done links to the activity, planned opens to its steps, else plain (#181)", () => {
  assert.equal(
    workoutRefHtml({ title: "Rit <1>", activity_id: "a/1" }, { note: " <i>n</i>" }),
    '<a href="#/activity/a%2F1" data-workout-activity>Rit &lt;1&gt;</a> <i>n</i>',
  );
  const planned = workoutRefHtml({ title: "Loop", workout: "- 10m Z2 HR" }, { label: (h) => `<b>${h}</b>` });
  assert.match(planned, /^<details class="collapsible workout-ref" data-workout-steps><summary><span><b>Loop<\/b><\/span><\/summary>/);
  assert.match(planned, /workout-steps/);
  // The activity wins over the workout text; a shared view keeps a done one plain.
  assert.match(workoutRefHtml({ title: "X", activity_id: 5, workout: "- 1m Z1" }), /^<a /);
  assert.equal(workoutRefHtml({ title: "X", activity_id: 5 }, { linkActivities: false }), "X");
  assert.equal(workoutRefHtml({ title: "X", activity_id: null, workout: "" }), "X");
});

test("a Strava id past 2^53 reaches the link exactly, because the API sends it as a string (#87)", () => {
  const body = '{"activity_id": "strava:9007199254740993", "rounded": 9007199254740993}';
  const parsed = JSON.parse(body);
  assert.equal(
    workoutRefHtml({ title: "Ride", activity_id: parsed.activity_id }),
    '<a href="#/activity/strava%3A9007199254740993" data-workout-activity>Ride</a>',
  );
  // What a number would have done: JSON.parse rounds it to another activity.
  assert.equal(String(parsed.rounded), "9007199254740992");
});
