// The start page's blocks: which exist, their default order, and how the saved order
// and hidden blocks (portal prefs dashboard_order / dashboard_hidden, kept on the server) are
// applied. No DOM here, so node --test can check it.
import { N } from "./i18n.js";

// Default order: advice and the check-in first (on a phone they must be on the first screen),
// then how you are, the week and what is coming, then the day's numbers, then the diary.
// wide: spans the page; the others share the width two by two on a large screen.
export const BLOCKS = [
  { id: "advice", label: N("Advice and plan"), wide: true },
  { id: "checkin", label: N("Check-in") },
  { id: "complaints", label: N("Complaints") },
  { id: "noted", label: N("Diary notes of the day") },
  { id: "recovery", label: N("Recovery") },
  { id: "week", label: N("This week") },
  { id: "upcoming", label: N("Coming up and calendar"), wide: true },
  { id: "numbers", label: N("Key numbers"), wide: true },
  { id: "sleep", label: N("Last night") },
  { id: "body", label: N("Body and measurements") },
  { id: "timeline", label: N("Timeline"), wide: true },
  { id: "bests", label: N("Best efforts") },
  { id: "other", label: N("Other measurements"), wide: true },
  { id: "diary", label: N("Diary") },
];

export const DEFAULT_ORDER = BLOCKS.map((block) => block.id);
const KNOWN = new Set(DEFAULT_ORDER);

/**
 * The blocks in the saved order: saved ids first (unknown ones dropped), then any block the
 * saved order doesn't list yet, at its default place relative to the block before it.
 */
export function blockOrder(saved) {
  const order = [];
  for (const id of Array.isArray(saved) ? saved : []) {
    if (KNOWN.has(id) && !order.includes(id)) order.push(id);
  }
  DEFAULT_ORDER.forEach((id, index) => {
    if (order.includes(id)) return;
    const before = DEFAULT_ORDER.slice(0, index).reverse().find((other) => order.includes(other));
    order.splice(before ? order.indexOf(before) + 1 : 0, 0, id);
  });
  return order;
}

/** [{...block, hidden}] in order, for the blocks this view can show (`available` ids). */
export function arrangedBlocks(prefs = {}, available = DEFAULT_ORDER) {
  const can = new Set(available);
  const hidden = new Set(Array.isArray(prefs.dashboard_hidden) ? prefs.dashboard_hidden : []);
  const byId = Object.fromEntries(BLOCKS.map((block) => [block.id, block]));
  return blockOrder(prefs.dashboard_order)
    .filter((id) => can.has(id))
    .map((id) => ({ ...byId[id], hidden: hidden.has(id) }));
}

/** The full order with `id` moved one place up (-1) or down (+1); unchanged at an edge. */
export function moveBlock(order, id, step) {
  const list = blockOrder(order);
  const from = list.indexOf(id);
  const to = from + step;
  if (from < 0 || to < 0 || to >= list.length) return list;
  [list[from], list[to]] = [list[to], list[from]];
  return list;
}

/**
 * Move within what the editor shows: `visible` is the order of the blocks this view lists.
 * Swaps `id` with its visible neighbour, so a block the view can't show never makes a move
 * look like it did nothing.
 */
export function moveVisible(order, visible, id, step) {
  const list = blockOrder(order);
  const at = visible.indexOf(id);
  const other = visible[at + step];
  if (at < 0 || other === undefined) return list;
  const a = list.indexOf(id);
  const b = list.indexOf(other);
  [list[a], list[b]] = [list[b], list[a]];
  return list;
}

/** The hidden list with `id` shown or hidden. */
export function toggleHidden(hidden, id, show) {
  const set = new Set(Array.isArray(hidden) ? hidden : []);
  if (show) set.delete(id);
  else set.add(id);
  return DEFAULT_ORDER.filter((known) => set.has(known));
}

/** Prefs changes that store `order`, or clear it when it is the default. */
export function orderChange(order) {
  const list = blockOrder(order);
  return list.join() === DEFAULT_ORDER.join() ? null : list;
}
