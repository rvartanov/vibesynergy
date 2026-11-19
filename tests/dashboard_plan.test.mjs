import assert from "node:assert/strict";
import test from "node:test";

// The page module touches window and document when it loads; a bare stub is enough here.
const noop = () => {};
const stubElement = () => ({ addEventListener: noop, setAttribute: noop, appendChild: noop, style: {}, classList: { add: noop, remove: noop, toggle: noop } });
globalThis.window ??= { addEventListener: noop, matchMedia: () => ({ matches: false, addEventListener: noop }), location: { hash: "" } };
globalThis.document ??= { addEventListener: noop, querySelector: () => null, querySelectorAll: () => [], createElement: stubElement, documentElement: stubElement(), body: stubElement() };
globalThis.localStorage ??= { getItem: () => null, setItem: noop, removeItem: noop };
globalThis.navigator ??= { language: "nl" };

const { planHtml } = await import("../src/vibesynergy/portal/static/js/pages/dashboard.js");

test("a planned workout with a description opens like in This week", () => {
  const html = planHtml(
    [
      {
        title: "Korte duurloop 40'",
        sport: "running",
        status: "today",
        description: "- 10m Z1\n- 25m Z2\n- 5m Z1",
      },
      { title: "Zwemmen duur 35'", sport: "swimming", status: "today" },
    ],
    [],
    null,
    "vibesynergy",
  );
  assert.match(html, /<details class="collapsible wp-details dash-workout"><summary>/);
  assert.match(html, /Korte duurloop 40&#39;/);
  assert.match(html, /25m|25 min/);
  // Without a description it stays a plain line.
  assert.equal((html.match(/dash-workout/g) || []).length, 1);
});

test("a done workout keeps its link to the activity", () => {
  const html = planHtml(
    [{ title: "Run", sport: "running", status: "done", description: "- 30m Z2", done_by: [{ id: "garmin:1" }] }],
    [],
    null,
    "vibesynergy",
  );
  assert.match(html, /href="#\/activity\/garmin%3A1"/);
});

test("a changed workout says why, without opening it", async () => {
  const { deviationText } = await import("../src/vibesynergy/portal/static/js/pages/dashboard.js");
  const html = planHtml(
    [{
      title: "Training geven 1u15",
      sport: "running",
      status: "changed",
      description: "- 75m Z1",
      deviation: { kind: "harder", above_s: 1680, ceiling: 1 },
      done_by: [{ id: "garmin:2" }],
    }],
    [],
    null,
    "vibesynergy",
  );
  assert.match(html, /dash-why">Harder than planned: 28 min above zone 1\./);
  assert.equal(deviationText({ kind: "shorter", planned_s: 3000, actual_s: 1800 }).startsWith("Shorter than planned"), true);
  assert.match(deviationText({ kind: "distance", planned_m: 10000, actual_m: 13000 }), /13/);
  assert.match(deviationText({ kind: "intensity", planned: "easy", done: "hard" }), /hard instead of easy/);
  assert.equal(deviationText(null), "Done differently from the plan.");
});

test("a done or changed workout shows Planned and Done, the reason on top (#116)", async () => {
  const { planLinesHtml, deviationText, statusWord } = await import("../src/vibesynergy/portal/static/js/plan_lines.js");
  const html = planHtml(
    [{
      title: "Zwemmen duur 40'",
      sport: "swimming",
      status: "changed",
      description: "- 300mtr Z1 Pace",
      deviation: { kind: "structure", planned: "3x300 Middel", done: "5x50 + 3x100" },
      comparison: { check: "differs", planned: "3x300 Middel", done: "5x50 + 3x100", rpe: 3 },
      done_by: [{ id: "garmin:25129577914", sport: "lap_swimming", moving_s: 4064.9, distance_m: 2525 }],
    }],
    [],
    null,
    "vibesynergy",
  );
  const why = html.indexOf("dash-why");
  const planned = html.indexOf("Planned:");
  const done = html.indexOf("Done:");
  assert.ok(why > 0 && why < planned && planned < done, html);
  assert.match(html, /Zwemmen duur 40&#39; — 3x300 Middel/);
  assert.match(html, /Swimming 2,?525 m, 68&#39; — 5x50 \+ 3x100 · RPE 3/);
  assert.match(deviationText({ kind: "structure", planned: "25' Z2", done: "4x5'" }), /Another workout than planned: 4x5' instead of 25' Z2\./);
  assert.match(deviationText({ kind: "structure", planned: "4x5' Z4", done_shape: "steady" }), /steady instead of 4x5' Z4/);
  // Done with thin laps: the word says so; an upcoming session gets no lines.
  assert.equal(statusWord("done", { check: "not_checked" }, "done"), "done, not checked");
  assert.equal(planLinesHtml({ title: "Run", status: "upcoming", actual: null }), "");
});
