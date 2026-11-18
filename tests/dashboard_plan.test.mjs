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
