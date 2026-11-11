// Compliance tone: within 80-120% as planned, 50-150% close, else far off (8-11).
import assert from "node:assert/strict";
import { test } from "node:test";

import { complianceTone } from "../src/vibesynergy/portal/static/js/compliance.js";

test("compliance tones", () => {
  assert.equal(complianceTone(102.7), "success");
  assert.equal(complianceTone(80), "success");
  assert.equal(complianceTone(124.9), "warning");
  assert.equal(complianceTone(55), "warning");
  assert.equal(complianceTone(18), "danger");
  assert.equal(complianceTone(160), "danger");
});
