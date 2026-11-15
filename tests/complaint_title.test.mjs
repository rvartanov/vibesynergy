import assert from "node:assert/strict";
import test from "node:test";

import { complaintTitle } from "../src/vibesynergy/portal/static/js/format.js";

test("a default complaint title follows the language; your own title stays", () => {
  // Without a catalogue t() returns the English text: the default reads as stored.
  assert.equal(complaintTitle({ title: "Left calf", body_part: "calf", side: "left" }), "Left calf");
  assert.equal(complaintTitle({ title: "Lower back", body_part: "lower_back", side: "none" }), "Lower back");
  assert.equal(complaintTitle({ title: "Shin splints", body_part: "shin", side: "left" }), "Shin splints");
  assert.equal(complaintTitle({ title: "Old title" }), "Old title");
  assert.equal(complaintTitle(null), "");
});
