import assert from "node:assert/strict";
import test from "node:test";

import { addDays, dayMonth, longDate, monthName, monthYear, parseDay, shortDate, weekday } from "../src/vibesynergy/portal/static/js/format.js";

test("a missing date is a dash, never an exception", () => {
  // Planning & progress crashed on longDate(null) for a user without a plan.
  for (const missing of [null, undefined, "", "not-a-date"]) {
    assert.ok(Number.isNaN(parseDay(missing).getTime()), String(missing));
    for (const text of [longDate, dayMonth, weekday, monthName, monthYear]) assert.equal(text(missing), "—", `${text.name}(${missing})`);
    assert.equal(addDays(missing, 1), "");
  }
  assert.equal(shortDate(null), "–");
});

test("a real date still reads as before", () => {
  assert.equal(parseDay("2026-09-25").toISOString(), "2026-09-25T00:00:00.000Z");
  assert.equal(parseDay("2026-09-25T10:00:00").toISOString(), "2026-09-25T00:00:00.000Z");
  assert.equal(longDate("2026-09-25"), "Friday 25 September 2026");
  assert.equal(dayMonth("2026-09-25"), "25 Sept");
  assert.equal(addDays("2026-09-30", 1), "2026-10-01");
});
