import assert from "node:assert/strict";
import test from "node:test";

import { gradeChip, messageIcon, sportChip, sportKey, sportLabel, tagChip, withSportIcon } from "../src/vibesynergy/portal/static/js/symbols.js";
import { weekProgressHtml } from "../src/vibesynergy/portal/static/js/week_progress.js";

test("VibeSynergy's sport names map onto the design system's sports", () => {
  const cases = {
    lap_swimming: "swim", open_water_swimming: "swim", swim: "swim",
    cycling: "bike", indoor_cycling: "bike", virtual_ride: "bike", ride: "bike", bike: "bike",
    running: "run", trail_running: "run", treadmill_running: "run", run: "run",
    strength_training: "strength", strength: "strength", pilates: "mobility", yoga: "mobility",
    walking: "walk", hiking: "walk", rowing: "other", "": "other",
  };
  for (const [type, sport] of Object.entries(cases)) assert.equal(sportKey(type), sport, type);
});

test("a sport chip has an aria-hidden icon and the written name", () => {
  const html = sportChip("trail_running", "Trail <run>");
  assert.match(html, /^<span class="ds-sport ds-sport--run"><svg class="ds-i"[^>]*aria-hidden="true"/);
  assert.ok(html.endsWith("Trail &lt;run&gt;</span>"));
  assert.equal(sportLabel("lap_swimming"), "sport.swim"); // no catalogue loaded: the key
  assert.match(sportChip("ride", "Ride", { hideName: true }), /<span class="visually-hidden">Ride<\/span>/);
});

test("the sport icon carries the sport's colour through data-sport", () => {
  assert.match(withSportIcon("cycling", "Ride"), /^<span data-sport="bike"><span class="sport-icon"><svg/);
  assert.match(messageIcon("race"), /aria-hidden="true"/);
  assert.equal(messageIcon("nope"), messageIcon("info"));
});

test("the week card shows each sport's icon and a toned status chip", () => {
  const html = weekProgressHtml({
    days_left: 2,
    counts: { done: 1 },
    sports: [{ family: "ride", done_s: 3600, goal_s: 7200, remaining: [], remaining_s: 0, planned_count: 1 }],
    sessions: [{ family: "ride", date: "2026-11-02", title: "Ride", status: "done", actual: { id: "a1", duration_s: 3600 } }],
    totals: {},
  });
  assert.match(html, /class="wp-sport" data-family="ride" data-sport="bike"/);
  assert.match(html, /<span class="wp-name"><span class="sport-icon"><svg class="ds-i"/);
  assert.match(html, /<span class="chip success">done<\/span>/);
});

test("diary tags and evidence grades are tone chips with their word, escaped", () => {
  assert.equal(tagChip("sick", "Ziek"), '<span class="chip danger">Ziek</span>');
  assert.equal(tagChip("travel", "Reizen"), '<span class="chip planned">Reizen</span>');
  assert.equal(tagChip("own_tag", "<b>x</b>"), '<span class="chip">&lt;b&gt;x&lt;/b&gt;</span>');
  assert.equal(gradeChip("A", "Strong evidence"), '<span class="chip success">Strong evidence</span>');
  assert.equal(gradeChip("D", "Weak evidence"), '<span class="chip warning">Weak evidence</span>');
  assert.equal(gradeChip("A", ""), "");
});
