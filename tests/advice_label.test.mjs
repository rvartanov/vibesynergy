import assert from "node:assert/strict";
import test from "node:test";

import {
  adviceHtml,
  adviceLevel,
  reportHeadHtml,
  tomorrowAdviceHtml,
  verdictHtml,
  viewHeadHtml,
} from "../src/vibesynergy/portal/static/js/daily_advice.js";
import { shortDay } from "../src/vibesynergy/portal/static/js/format.js";

// #138: an advice is named after the day it is for, never the day of its run.
// #147: the words come from the server's one view (query/advice_view.py); pages build none.

const COLOUR = {
  entry_id: 1073,
  date: "2026-11-21",
  label: "Advice for today – Sat 21 Nov",
  stamp: "Updated 09:21, your night is in",
  state: "colour",
  colour: "amber",
  colour_name: "Amber",
  headline: "Take it a bit easier",
  verdict: "Amber – Take it a bit easier",
  recovery: "",
  status: "replaced",
  current_id: 1074,
};
const DONE = {
  ...COLOUR,
  entry_id: 1074,
  stamp: "Updated 12:48, after your workout",
  state: "done",
  headline: "Done for today",
  verdict: "Done for today",
  recovery: "Recovery: amber – the week's HRV is still below your band.",
  status: "current",
  current_id: 1074,
};

test("the short day reads like the server's (format_user_short_day)", () => {
  assert.equal(shortDay("2026-11-21"), "Sat 21 Nov");
  assert.equal(shortDay(null), "—");
});

test("the colour state: a colour and its meaning, or done for today with the recovery", () => {
  const colour = verdictHtml(COLOUR);
  assert.match(colour, /data-advice-state="colour"/);
  assert.match(colour, /chip warning">Amber</);
  assert.match(colour, /Take it a bit easier/);
  const done = verdictHtml(DONE);
  assert.match(done, /data-advice-state="done"/);
  assert.match(done, /Done for today/);
  assert.match(done, /Recovery: amber – /);
  assert.doesNotMatch(done, /Take it a bit easier/);
  assert.equal(verdictHtml({ ...COLOUR, state: "none" }), "");
});

test("the card always shows the current colour, also once the day is done (#217)", () => {
  assert.equal(adviceLevel({ level: "amber", view: DONE }), "amber");
  assert.equal(adviceLevel({ level: "green", view: { ...DONE, colour: "red" } }), "red");
  assert.equal(adviceLevel({ level: "amber", view: COLOUR }), "amber");
  assert.equal(adviceLevel({ level: "green" }), "green");
});

test("no colour known: no colour class, so the edge is neutral, never green (#217)", () => {
  assert.equal(adviceLevel({ error: "unavailable" }), "");
  assert.equal(adviceLevel(null), "");
  assert.equal(adviceLevel({ mode: "off", level: null, view: null }), "");
  assert.equal(adviceLevel({ level: null, view: { ...COLOUR, state: "none", colour: null } }), "");
  assert.equal(adviceLevel({ level: "toString" }), "");
});

test("an advice without a colour says why in a short line (#217)", () => {
  const none = adviceHtml({ mode: "ai", level: null, advice: "Rest well.", view: null });
  assert.match(none, /data-advice-no-colour/);
  const amber = adviceHtml({ mode: "rules", level: "amber", advice: "Easy today." });
  assert.doesNotMatch(amber, /data-advice-no-colour/);
  assert.match(adviceHtml({ mode: "off" }), /data-advice-off/);
});

test("an entry's head is its label and stamp, with no run in it", () => {
  const head = viewHeadHtml(DONE);
  assert.match(head, /data-advice-label="2026-11-21">Advice for today – Sat 21 Nov</);
  assert.match(head, /Updated 12:48, after your workout/);
  assert.doesNotMatch(head, /run/);
  assert.equal(viewHeadHtml(null), "");
});

test("the report page: label, stamp, colour state, earlier versions; a replaced one says so", () => {
  const versions = [
    { id: 1074, at: "2026-11-21T11:48:22Z", valid: true, colour: "amber", status: "current", advice: "Done.", why: "after your workout" },
    { id: 1073, at: "2026-11-21T08:21:51Z", valid: true, colour: "amber", status: "replaced", advice: "Easy ride.", why: "your night is in" },
  ];
  const current = reportHeadHtml(DONE, versions);
  assert.match(current, /<h2 data-advice-label="2026-11-21">Advice for today – Sat 21 Nov<\/h2>/);
  assert.match(current, /Earlier versions \(1\)/);
  assert.match(current, /href="#\/coach\/report\/1073"/);
  assert.match(current, /your night is in/);
  assert.doesNotMatch(current, /Version \d/);
  assert.doesNotMatch(current, /data-thread-status="/);
  const old = reportHeadHtml(COLOUR, versions);
  assert.match(old, /data-thread-status="replaced"/);
  assert.match(old, /href="#\/coach\/report\/1074"/);
  assert.equal(reportHeadHtml(null), "");
});

test("tomorrow's card: the view's label, colour, first line, stamp and the notice's link", () => {
  const view = { ...COLOUR, label: "Advice for tomorrow – Sat 21 Nov", stamp: "Written 21:46, evening preview" };
  const html = tomorrowAdviceHtml({ date: "2026-11-21", level: "amber", advice: "Morgen de rustige rit van 1u15.", entry_id: 1069, link: "#/coach/report/1069", view });
  assert.match(html, /data-advice-tomorrow="2026-11-21"/);
  assert.match(html, /Advice for tomorrow – Sat 21 Nov/);
  assert.match(html, /verdict amber/);
  assert.match(html, /Amber/);
  assert.match(html, /Morgen de rustige rit van 1u15\./);
  assert.match(html, /Written 21:46, evening preview/);
  assert.match(html, /href="#\/coach\/report\/1069"/);
  assert.equal(tomorrowAdviceHtml(null), "");
});
