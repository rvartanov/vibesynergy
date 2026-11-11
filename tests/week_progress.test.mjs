import assert from "node:assert/strict";
import test from "node:test";

import { weekProgressHtml, weekSummary } from "../src/vibesynergy/portal/static/js/week_progress.js";

function sport(family, done, goal, remaining = []) {
  const left = remaining.reduce((sum, r) => sum + r.duration_s, 0);
  return { family, done_s: done, goal_s: goal, target_s: goal, remaining, remaining_s: left, planned_count: remaining.length };
}

test("advice first: a missed session is not made up", () => {
  const text = weekSummary({ days_left: 3, counts: { missed: 1 }, sports: [sport("run", 0, 3600)] });
  assert.match(text, /missed\. Don't make it up/);
});

test("on track names what is still to do", () => {
  const progress = {
    days_left: 3,
    counts: { done: 1 },
    sports: [sport("ride", 3600, 7200, [{ date: "2026-11-05", title: "Ride", duration_s: 3600 }])],
    totals: { remaining_s: 3600 },
  };
  assert.match(weekSummary(progress), /^On track\. Still to do: 1 session, 1 h 00 min\.$/);
});

test("a sport that cannot reach its goal is named", () => {
  const progress = { days_left: 0, counts: { done: 2 }, sports: [sport("swim", 1800, 4500)] };
  assert.match(weekSummary(progress), /Below this week's goal: Swimming/);
});

test("the card escapes plan text and has one row per sport", () => {
  const html = weekProgressHtml({
    days_left: 2,
    counts: {},
    note: { title: "Wk 1 <b>", focus: "Easy" },
    sports: [sport("run", 0, 3600), sport("swim", 0, 1800)],
    sessions: [],
    totals: {},
  });
  assert.ok(html.includes("Wk 1 &lt;b&gt;"));
  assert.equal(html.match(/class="wp-sport"/g).length, 2);
});
