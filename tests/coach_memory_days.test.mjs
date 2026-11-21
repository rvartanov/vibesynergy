import assert from "node:assert/strict";
import test from "node:test";

import { namesDay, withDays } from "../src/vibesynergy/portal/static/js/coach_settings.js";

// #139: notes no longer ask for days; an older note's days go into its text once.
test("a note that already names its day stays as it is", () => {
  const text = "Vrijdag is een optiedag: rust, pilates of zwemmen.";
  assert.equal(withDays(text, [5]), text);
  assert.ok(namesDay("Op vrijdagen geen kracht", 5));
  assert.ok(namesDay("Swim on Fri", 5));
});

test("a day the text does not name is added once", () => {
  const once = withDays("Geen kracht", [2, 4]);
  assert.equal(once, "Geen kracht (Tue, Thu)");
  assert.equal(withDays(once, [2, 4]), once);
});

test("no days, no change", () => {
  assert.equal(withDays("Ik train 's ochtends", []), "Ik train 's ochtends");
  assert.equal(withDays("Ik train 's ochtends", undefined), "Ik train 's ochtends");
  // "do" inside a word is not Thursday, "zondags" is Sunday.
  assert.ok(!namesDay("doen", 4));
  assert.ok(namesDay("zondags lang", 7));
});
