import assert from "node:assert/strict";
import test from "node:test";

import {
  adviceDayLabel,
  adviceRunLabel,
  entryAdviceLabel,
  tomorrowAdviceHtml,
} from "../src/vibesynergy/portal/static/js/daily_advice.js";
import { shortDay } from "../src/vibesynergy/portal/static/js/format.js";

// #138: an advice is named after the day it is for, never the day of its run.

test("the short day reads like the server's (format_user_short_day)", () => {
  assert.equal(shortDay("2026-11-21"), "Sat 21 Nov");
  assert.equal(shortDay("2026-11-23"), "Mon 23 Nov");
  assert.equal(shortDay(null), "—");
});

test("the day label is said from today", () => {
  assert.equal(adviceDayLabel("2026-11-21", "2026-11-20"), "Advice for tomorrow – Sat 21 Nov");
  assert.equal(adviceDayLabel("2026-11-20", "2026-11-20"), "Advice for today – Fri 20 Nov");
  assert.equal(adviceDayLabel("2026-11-19", "2026-11-20"), "Advice for yesterday – Thu 19 Nov");
  assert.equal(adviceDayLabel("2026-11-01", "2026-11-20"), "Advice for Sun 1 Nov");
});

test("the run that wrote it stays beside it", () => {
  assert.equal(adviceRunLabel("evening", "2026-11-20T20:46:00Z"), "evening run 21:46");
  assert.equal(adviceRunLabel("checkin", "2026-11-20T07:53:00Z"), "check-in run 08:53");
  assert.equal(adviceRunLabel(null, "2026-11-20T07:53:00Z"), "written 08:53");
});

test("an evening entry of Friday is labelled Saturday; other entries keep no label", () => {
  const evening = { advice_day: "2026-11-21", advice_job: "evening", at: "2026-11-20T20:46:00Z", for_date: "2026-11-20" };
  assert.equal(entryAdviceLabel(evening, "2026-11-20"), "Advice for tomorrow – Sat 21 Nov (evening run 21:46)");
  assert.equal(entryAdviceLabel({ entry_type: "weekly", at: "2026-11-20T20:46:00Z" }, "2026-11-20"), "");
});

test("tomorrow's card: its label, colour, first line, run and the notice's link", () => {
  const html = tomorrowAdviceHtml(
    { date: "2026-11-21", level: "amber", advice: "Morgen de rustige rit van 1u15.", entry_id: 1069, job: "evening", at: "2026-11-20T20:46:00Z", link: "#/coach/report/1069" },
    "2026-11-20",
  );
  assert.match(html, /data-advice-tomorrow="2026-11-21"/);
  assert.match(html, /Advice for tomorrow – Sat 21 Nov/);
  assert.match(html, /verdict amber/);
  assert.match(html, /Amber/);
  assert.match(html, /Morgen de rustige rit van 1u15\./);
  assert.match(html, /evening run 21:46/);
  assert.match(html, /href="#\/coach\/report\/1069"/);
  assert.equal(tomorrowAdviceHtml(null, "2026-11-20"), "");
});
