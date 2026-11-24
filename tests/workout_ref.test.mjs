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
