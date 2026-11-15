import assert from "node:assert/strict";
import test from "node:test";

import { recoverySummaryText } from "../src/vibesynergy/portal/static/js/recovery_summary.js";

test("the recovery line agrees with today's advice", () => {
  assert.equal(recoverySummaryText(0, 3, "green"), "You look recovered as usual.");
  // Resting heart rate alone: the advice stays green, so no "take it easier" here (#37).
  assert.equal(recoverySummaryText(1, 3, "green"), "1 of 3 signs is off your usual. Keep an eye on it; on its own it doesn't change today's advice.");
  assert.equal(recoverySummaryText(2, 3, "amber"), "2 of 3 signs are off your usual. Take it a little easier.");
  assert.equal(recoverySummaryText(1, 3, "red"), "1 of 3 signs is off your usual. Take it a little easier.");
});
