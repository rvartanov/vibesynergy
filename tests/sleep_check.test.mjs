import assert from "node:assert/strict";
import test from "node:test";

// The page module touches window and document when it loads; a bare stub is enough here.
const noop = () => {};
const stubElement = () => ({ addEventListener: noop, setAttribute: noop, appendChild: noop, style: {}, classList: { add: noop, remove: noop, toggle: noop } });
globalThis.window ??= { addEventListener: noop, matchMedia: () => ({ matches: false, addEventListener: noop }), location: { hash: "" } };
globalThis.document ??= { addEventListener: noop, querySelector: () => null, querySelectorAll: () => [], createElement: stubElement, documentElement: stubElement(), body: stubElement() };
globalThis.localStorage ??= { getItem: () => null, setItem: noop, removeItem: noop };
globalThis.navigator ??= { language: "en" };

const { sleepCheckHtml } = await import("../src/vibesynergy/portal/static/js/pages/dashboard.js");

// 17-11-2026: the night as the sleep check (#100) marks it.
const BAD = {
  state: "bad",
  auto_state: "bad",
  override: null,
  headline: "Sleep may be badly recorded",
  reasons: ["no REM sleep recorded in a night of 10 h 55 min", "HRV measured only from 20:22 to 00:07"],
  effect: "Its sleep, HRV and readiness don't count for the advice.",
};

test("a marked night says why and asks: yes, incomplete or no, it's right", () => {
  const html = sleepCheckHtml({ sleep_check: BAD });
  assert.match(html, /<strong>Sleep may be badly recorded<\/strong>: no REM sleep recorded/);
  assert.match(html, /don&#39;t count for the advice/);
  assert.match(html, /data-sleep-incomplete="true"/);
  assert.match(html, /data-sleep-incomplete="false"/);
});

test("a suspect night still counts, and says so", () => {
  const html = sleepCheckHtml({ sleep_check: { ...BAD, state: "suspect", auto_state: "suspect", headline: "Sleep measurement may be off", effect: undefined } });
  assert.match(html, /It still counts for now/);
});

test("the athlete's own choice is shown with an undo", () => {
  const right = sleepCheckHtml({ sleep_check: { ...BAD, state: "ok", override: "ok", effect: undefined } });
  assert.match(right, /measured right/);
  assert.match(right, /data-sleep-incomplete="null"/);
  assert.doesNotMatch(right, /data-sleep-incomplete="true"/);
  const marked = sleepCheckHtml({ sleep_check: { ...BAD, state: "incomplete", override: "incomplete" } });
  assert.match(marked, /You marked last night&#39;s sleep as incomplete/);
  assert.match(marked, /data-sleep-incomplete="null"/);
});

test("nothing to say, nothing shown", () => {
  assert.equal(sleepCheckHtml({ sleep_check: null }), "");
  assert.equal(sleepCheckHtml(null), "");
});
