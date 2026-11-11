import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_ORDER,
  arrangedBlocks,
  blockOrder,
  moveBlock,
  moveVisible,
  orderChange,
  toggleHidden,
} from "../src/vibesynergy/portal/static/js/start_layout.js";

test("no saved order gives the default, advice and check-in first", () => {
  assert.deepEqual(blockOrder(undefined), DEFAULT_ORDER);
  assert.deepEqual(DEFAULT_ORDER.slice(0, 2), ["advice", "checkin"]);
});

test("saved order wins; unknown and repeated ids are dropped; new blocks keep their place", () => {
  const saved = ["body", "gone", ...DEFAULT_ORDER.filter((id) => id !== "body" && id !== "bests"), "body"];
  const order = blockOrder(saved);
  assert.equal(order[0], "body");
  assert.equal(order[1], "advice");
  // A block the saved order doesn't know yet (bests) follows the block before it by default.
  assert.equal(order[order.indexOf("timeline") + 1], "bests");
  assert.equal(order.length, DEFAULT_ORDER.length);
  assert.equal(new Set(order).size, order.length);
});

test("arranged blocks follow the order, mark hidden ones and keep only what the view can show", () => {
  const blocks = arrangedBlocks(
    { dashboard_order: ["week", ...DEFAULT_ORDER.filter((id) => id !== "week")], dashboard_hidden: ["advice", "nonsense"] },
    ["advice", "week", "numbers"],
  );
  assert.deepEqual(blocks.map((b) => b.id), ["week", "advice", "numbers"]);
  assert.deepEqual(blocks.map((b) => b.hidden), [false, true, false]);
  assert.equal(blocks[1].wide, true);
});

test("moving up and down, with the edges left alone", () => {
  assert.deepEqual(moveBlock(null, "checkin", -1).slice(0, 2), ["checkin", "advice"]);
  assert.deepEqual(moveBlock(null, "advice", -1), DEFAULT_ORDER);
  assert.deepEqual(moveBlock(null, DEFAULT_ORDER.at(-1), 1), DEFAULT_ORDER);
});

test("moving within the listed blocks skips blocks the view does not show", () => {
  const visible = ["advice", "numbers", "diary"];
  const moved = moveVisible(null, visible, "numbers", -1);
  assert.ok(moved.indexOf("numbers") < moved.indexOf("advice"));
  assert.deepEqual(moveVisible(null, visible, "diary", 1), DEFAULT_ORDER);
});

test("hide and show again; default order is stored as nothing", () => {
  assert.deepEqual(toggleHidden(["timeline"], "body", false), ["body", "timeline"]);
  assert.deepEqual(toggleHidden(["timeline", "body"], "timeline", true), ["body"]);
  assert.equal(orderChange(DEFAULT_ORDER), null);
  assert.deepEqual(orderChange(moveBlock(null, "checkin", -1)).slice(0, 2), ["checkin", "advice"]);
});
