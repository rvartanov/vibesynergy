import assert from "node:assert/strict";
import test from "node:test";

import { parseExercise, parseWorkout, workoutHtml } from "../src/vibesynergy/portal/static/js/workout_text.js";

const SWIM = `Techniek, rustig. Licht = RPE 3-4.

Warmup
- Licht 300mtr Z1 Pace

Main set
8x
- Middel drill 50mtr Z2 Pace
- 20s intensity=rest

Cooldown
- Licht 200mtr Z1 Pace`;

const STRENGTH = `Sessie A: 3 sets van 10-12 op RPE 7-8.

[garmin]
warmup time=5m "5' rustig fietsen"
SQUAT/BARBELL_BACK_SQUAT sets=3 reps=10 rest=120s weight=60kg "Back squat 60 kg"
ROW/INVERTED_ROW sets=3 reps=10 rest=75s weight=bw
PLANK/SIDE_PLANK sets=3 time=30s side=both rest=30s "Zijplank"

- 40m free`;

test("sections, repeats and steps come apart; the intro stays text", () => {
  const types = parseWorkout(SWIM).filter((b) => b.type !== "gap").map((b) => b.type);
  assert.deepEqual(types, ["text", "head", "steps", "head", "head", "steps", "head", "steps"]);
  const html = workoutHtml(SWIM);
  assert.match(html, /^<div class="plan-note workout-text"><p>Techniek, rustig\. Licht = RPE 3-4\.<\/p>/);
  assert.match(html, /<b>8× repeat<\/b>/);
  // zone: swatch + label, the name for screen readers; rest in words
  assert.match(html, /Licht 300mtr <span class="zone-badge"><i class="zone-swatch"[^>]*aria-hidden="true"><\/i>/);
  assert.match(html, /<span class="visually-hidden">Zone 2<\/span><\/span> Pace/);
  assert.match(html, /<li>20s rest<\/li>/);
});

test("the strength block reads as exercises", () => {
  assert.deepEqual(parseExercise('SQUAT/BARBELL_BACK_SQUAT sets=3 reps=10 weight=60kg "Back squat"'), {
    name: "SQUAT/BARBELL_BACK_SQUAT",
    label: "Back squat",
    options: { sets: "3", reps: "10", weight: "60kg" },
  });
  const html = workoutHtml(STRENGTH);
  assert.match(html, /<li>5&#39; rustig fietsen <span class="sub">5 min<\/span><\/li>/);
  assert.match(html, /<li>Back squat 60 kg <span class="sub">3 × 10 · 60 kg · rest 2 min<\/span><\/li>/);
  assert.match(html, /<li>inverted row <span class="sub">3 × 10 · bodyweight · rest 75 s<\/span><\/li>/);
  assert.match(html, /<li>Zijplank <span class="sub">3 × 30 s · both sides · rest 30 s<\/span><\/li>/);
  assert.match(html, /<ul class="workout-steps"><li>40m free<\/li><\/ul>/);
});

test("free text stays as written and everything is escaped", () => {
  assert.equal(workoutHtml("Rustig <b>los</b>\nmet vrienden"), '<div class="plan-note">Rustig &lt;b&gt;los&lt;/b&gt;<br>met vrienden</div>');
  assert.doesNotMatch(workoutHtml('- 10m <img src=x> Z2\n[garmin]\nX sets=1 "<script>"'), /<img|<script>/);
});
