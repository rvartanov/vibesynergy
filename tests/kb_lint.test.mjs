import assert from "node:assert/strict";
import test from "node:test";

globalThis.navigator ??= { language: "en" };
globalThis.localStorage ??= { getItem: () => null, setItem: () => {}, removeItem: () => {} };

const { lintCard, lintText } = await import("../src/vibesynergy/portal/static/js/kb_lint.js");

// The live knowledge base on 20-11-2026 (#120).
const RANGE = { check: "rule_range", kind: "article", ref: "ai-coach-loop", article: "ai-coach-loop", names: ["ICU-1..4"], expected: ["ICU-1..6"] };
const TOOL = { check: "unknown_tool", kind: "article", ref: "coach-prompt-morning", article: "coach-prompt-morning", names: ["SendUserMessage"] };
const RULE = { check: "fixed_threshold", kind: "rule", ref: "ICU-5", article: "intervals-icu-workout-authoring", names: ["FTP 265", "2:30/100 m"] };

test("each check reads as a sentence with what was found", () => {
  assert.equal(lintText(RANGE), "ai-coach-loop refers to ICU-1..4, but the rules in use are ICU-1..6.");
  assert.equal(lintText(TOOL), "coach-prompt-morning names a tool that does not exist: SendUserMessage.");
  assert.match(lintText(RULE), /^ICU-5 has thresholds as fixed numbers: FTP 265, 2:30\/100 m\./);
  for (const check of ["model_name", "retired_name", "unknown_rule"]) {
    assert.doesNotMatch(lintText({ check, ref: "x", names: ["Y"] }), /\{/, check);
  }
  assert.equal(lintText({ check: "new_check", ref: "x", names: ["a", "b"] }), "x: a, b");
});

test("the card links each warning to its article and is empty without warnings", () => {
  assert.equal(lintCard([]), "");
  assert.equal(lintCard(undefined), "");
  const html = lintCard([RANGE, RULE]);
  assert.match(html, /notice warning/);
  assert.match(html, /href="#\/coach\/methods\/ai-coach-loop"/);
  assert.match(html, /href="#\/coach\/methods\/intervals-icu-workout-authoring"/);
  assert.equal((html.match(/<li /g) || []).length, 2);
});
