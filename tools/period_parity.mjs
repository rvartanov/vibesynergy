import assert from "node:assert/strict";
import { resolvePeriod } from "../src/vibesynergy/portal/static/js/period_picker.js";

const today = process.argv[2];
const cases = JSON.parse(process.argv[3]);
for (const c of cases) {
  const value = c.period ? { period: c.period } : { start: c.start, end: c.end };
  const got = resolvePeriod(value, today);
  const label = c.period || `${c.start}..${c.end}`;
  assert.deepEqual(got, { start: c.expect_start, end: c.expect_end }, label);
}
