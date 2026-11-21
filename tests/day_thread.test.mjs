import assert from "node:assert/strict";
import test from "node:test";

import {
  becauseText,
  continuityHtml,
  reportHeadHtml,
  versionsHtml,
} from "../src/vibesynergy/portal/static/js/daily_advice.js";

// #115: the day thread on the Coach page and the report page a notice links to.

const versions = [
  { id: 1053, version: 2, of: 2, status: "current", valid: true, colour: "green", at: "2026-11-19T08:57:00Z", advice: "Rustpols weer normaal.", changes: ["hrv"], differences: [], changed_because: { previous: 1018, reason: "Rustpols is weer normaal." } },
  { id: 1018, version: 1, of: 2, status: "replaced", replaced_by: 1053, valid: true, colour: "amber", at: "2026-11-19T07:00:00Z", advice: "Rustpols derde dag hoger.", changes: [], differences: [], first: true, changed_because: null },
];

test("a later version says why it changed, in one sentence", () => {
  assert.equal(becauseText(versions[0]), "Changed because: Rustpols is weer normaal.");
  assert.equal(becauseText(versions[1]), "");
});

test("an earlier version is marked replaced and linked to its own report, without a number", () => {
  const html = versionsHtml({ source: "ai", entry: { id: 1053 }, versions });
  assert.match(html, /data-version-status="replaced"/);
  assert.match(html, /Replaced/);
  assert.doesNotMatch(html, /Version \d/); // #147: the times say the order
  assert.match(html, /href="#\/coach\/report\/1018"/);
  assert.doesNotMatch(html, /data-advice-version="1053"/); // the current one is the card itself
});

test("the report page says whether a version is current or replaced", () => {
  const view = { entry_id: 1053, date: "2026-11-19", label: "Advice for today – Thu 19 Nov", stamp: "Updated 09:57, your night is in", state: "colour", colour: "green", colour_name: "Green", headline: "Well recovered", verdict: "Green – Well recovered", status: "current", current_id: 1053 };
  const current = reportHeadHtml(view, versions);
  assert.match(current, /Advice for today – Thu 19 Nov/);
  assert.match(current, /Updated 09:57, your night is in/);
  assert.doesNotMatch(current, /data-thread-status="/);
  const old = reportHeadHtml({ ...view, entry_id: 1018, status: "replaced" }, versions);
  assert.match(old, /data-thread-status="replaced"/);
  assert.match(old, /not the current advice/);
  assert.match(old, /href="#\/coach\/report\/1053"/);
  assert.equal(reportHeadHtml(null), "");
});

test("the continuity check shows only when there is a gap", () => {
  assert.equal(continuityHtml([]), "");
  const html = continuityHtml([{ rule: "notice_without_entry", message: "A notice shows a text that is not stored." }]);
  assert.match(html, /data-continuity-rule="notice_without_entry"/);
});
