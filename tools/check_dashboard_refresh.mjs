import assert from "node:assert/strict";

import { earlierAdviceStatus, savedAdviceRole } from "../src/vibesynergy/portal/static/js/coaching_presentation.js";
import { calendarDay, shouldAutoRefresh } from "../src/vibesynergy/portal/static/js/refresh_policy.js";

const minute = 60_000;
const base = {
  now: 1_000_000,
  renderedAt: 1_000_000,
  refreshMin: 5,
  hidden: false,
  pageAllowed: true,
  dayChanged: false,
};

assert.equal(shouldAutoRefresh(base), false);
assert.equal(shouldAutoRefresh({ ...base, now: base.renderedAt + 5 * minute - 15_000 }), true);
assert.equal(shouldAutoRefresh({ ...base, now: base.renderedAt + 5 * minute - 15_001 }), false);
assert.equal(shouldAutoRefresh({ ...base, now: base.renderedAt + 5 * minute, hidden: true }), false);
assert.equal(shouldAutoRefresh({ ...base, now: base.renderedAt + 60 * minute, refreshMin: 0 }), false);
assert.equal(shouldAutoRefresh({ ...base, dayChanged: true, refreshMin: 0 }), false);
assert.equal(shouldAutoRefresh({ ...base, dayChanged: true }), true);
assert.equal(shouldAutoRefresh({ ...base, dayChanged: true, hidden: true }), false);
assert.equal(shouldAutoRefresh({ ...base, dayChanged: true, pageAllowed: false }), false);
assert.equal(shouldAutoRefresh({ ...base, pageAllowed: false, now: base.renderedAt + 30 * minute }), false);

const beforeMidnight = new Date("2026-10-05T21:30:00Z");
const afterMidnight = new Date("2026-10-05T22:30:00Z");
assert.equal(calendarDay("Europe/Amsterdam", beforeMidnight), "2026-10-05");
assert.equal(calendarDay("Europe/Amsterdam", afterMidnight), "2026-10-06");
assert.equal(calendarDay("UTC", afterMidnight), "2026-10-05");

const advice = { summary: "Keep the planned session." };
assert.equal(savedAdviceRole({ advice: { ...advice, current: true }, state: "ready" }), "current");
assert.equal(
  savedAdviceRole({ advice: { ...advice, current: false }, state: "stale" }),
  "earlier",
);
assert.equal(
  savedAdviceRole({
    advice: { ...advice, current: true },
    state: "ready",
    display: { archive: true },
  }),
  "archive",
);
assert.equal(savedAdviceRole({ advice: null, state: "missing" }), "status");
assert.equal(savedAdviceRole({ advice: { ...advice, current: false }, state: "running" }), "earlier");
assert.equal(earlierAdviceStatus({ state: "running" }), "preparing");
assert.equal(earlierAdviceStatus({ state: "failed" }), "failed");
assert.equal(earlierAdviceStatus({ state: "stale" }), "outdated");
assert.equal(
  savedAdviceRole({ advice: { ...advice, current: true }, state: "failed" }),
  "current",
);
