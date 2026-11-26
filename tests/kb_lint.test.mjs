import assert from "node:assert/strict";
import test from "node:test";

globalThis.navigator ??= { language: "en" };
globalThis.localStorage ??= { getItem: () => null, setItem: () => {}, removeItem: () => {} };

const { fixKinds, lintCard, lintText } = await import("../src/vibesynergy/portal/static/js/kb_lint.js");

// The live knowledge base on 20-11-2026 (#120).
const RANGE = { check: "rule_range", kind: "article", ref: "ai-coach-loop", article: "ai-coach-loop", names: ["ICU-1..4"], expected: ["ICU-1..6"] };
const TOOL = { check: "unknown_tool", kind: "article", ref: "coach-prompt-morning", article: "coach-prompt-morning", names: ["SendUserMessage"] };
const RULE = { check: "fixed_threshold", kind: "rule", ref: "ICU-5", article: "intervals-icu-workout-authoring", names: ["FTP 300", "2:30/100 m"] };

test("each check reads as a sentence with what was found", () => {
  assert.equal(lintText(RANGE), "ai-coach-loop refers to ICU-1..4, but the rules in use are ICU-1..6.");
  assert.equal(lintText(TOOL), "coach-prompt-morning names a tool that does not exist: SendUserMessage.");
  assert.match(lintText(RULE), /^ICU-5 has thresholds as fixed numbers: FTP 300, 2:30\/100 m\./);
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

// What VibeSynergy fixed itself (#137).
const FIXED = {
  id: "kbprop_1", kind: "article", target: "ai-coach-loop", title: "Werkwijze AI-coach",
  article: "ai-coach-loop", at: "2026-11-21T06:00:00+00:00", version: 17, base_version: 16,
  state: "current", fixes: [{ field: "body_md", check: "rule_range", found: "ICU-1..4", fixed: "ICU-1..6" }],
};

test("the card lists the fixes made without asking, each with an undo", () => {
  const html = lintCard([], [FIXED]);
  assert.match(html, /data-kb-editorial/);
  assert.match(html, /ICU-1\.\.4 → ICU-1\.\.6/);
  assert.match(html, /data-editorial-undo data-kind="article" data-target="ai-coach-loop" data-version="16"/);
  assert.match(html, /href="#\/coach\/log\?kind=article&amp;target=ai-coach-loop"/);
  assert.doesNotMatch(html, /notice warning/);
  assert.doesNotMatch(lintCard([], [{ ...FIXED, state: "undone" }]), /data-editorial-undo/);
  assert.doesNotMatch(lintCard([], [FIXED], { canUndo: false }), /data-editorial-undo/);
  assert.equal(fixKinds(["retired_name", "rule_range"]), "outdated names, rule ranges");
});
