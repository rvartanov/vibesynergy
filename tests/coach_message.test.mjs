import assert from "node:assert/strict";
import test from "node:test";

import { entryTone, sessionsHtml, splitCurrent } from "../src/vibesynergy/portal/static/js/coach_message.js";

test("a verdict per session is a chip with a word, the title escaped", () => {
  const html = sessionsHtml([
    { title: "Duurloop <Z2>", decision: "keep" },
    { title: "Kracht", decision: "adjust", note: "RDL 50 kg" },
    { title: "Nope", decision: "maybe" },
  ]);
  assert.equal(
    html,
    '<ul class="coach-sessions"><li><span class="chip planned">Keep</span> Duurloop &lt;Z2&gt;</li>'
      + '<li><span class="chip advice">Adjust</span> Kracht <span class="sub">RDL 50 kg</span></li></ul>',
  );
  assert.equal(sessionsHtml(undefined), "");
  assert.equal(entryTone("morning"), "advice");
  assert.equal(entryTone("whatever"), "info");
});

test("now holds today and yesterday, plus open proposals of any age", () => {
  const entries = [
    { id: 1, for_date: "2026-11-06" },
    { id: 2, for_date: "2026-11-05" },
    { id: 3, for_date: "2026-11-04" },
    { id: 4, for_date: "2026-10-30", entry_type: "threshold_change", meta: { status: "proposed" } },
  ];
  const { current, archive } = splitCurrent(entries, "2026-11-06");
  assert.deepEqual(current.map((e) => e.id), [1, 2, 4]);
  assert.deepEqual(archive.map((e) => e.id), [3]);
});
