import assert from "node:assert/strict";
import test from "node:test";

import { setupGuideHtml } from "../src/vibesynergy/portal/static/js/setup_guide.js";

const state = (steps, more = {}) => ({ steps, complete: Object.values(steps).every(Boolean), dismissed: false, ...more });

test("the guide shows three steps with their state and one main action", () => {
  const html = setupGuideHtml(state({ source: true, zones: false, coach: false }));
  assert.equal((html.match(/<li class="setup-step/g) || []).length, 3);
  assert.equal((html.match(/chip success/g) || []).length, 1);
  assert.match(html, /1 of 3 done/);
  // Exactly one primary button: the first step still to do (zones).
  assert.equal((html.match(/class="btn primary"/g) || []).length, 1);
  assert.match(html, /class="btn primary" href="#\/zones"/);
  for (const href of ["#/settings/data/sources", "#/zones", "#/coach/settings"]) assert.ok(html.includes(`href="${href}"`), href);
  assert.match(html, /data-setup-dismiss/);
});

test("no guide when setup is complete, put away or unknown", () => {
  assert.equal(setupGuideHtml(state({ source: true, zones: true, coach: true })), "");
  assert.equal(setupGuideHtml(state({ source: false, zones: false, coach: false }, { dismissed: true })), "");
  assert.equal(setupGuideHtml(null), "");
});
