import assert from "node:assert/strict";
import { PARENT, REDIRECTS, SECTIONS, layout, markCurrent } from "../src/vibesynergy/portal/static/js/nav.js";

const today = SECTIONS.find((s) => s.id === "today");
assert.ok(today, "today section");
assert.equal(today.label, "Dashboard"); //
// One start page since Diary and Calendar are sections of it.
assert.deepEqual(
  today.pages.map((p) => p[0]),
  ["dashboard"],
);
assert.ok(!SECTIONS.some((s) => s.id === "data"));
const training = SECTIONS.find((s) => s.id === "training");
assert.ok(training && !training.pages.some((p) => p[0] === "calendar"));
// Training and Training plan merged into Planning & progress; old routes redirect.
assert.ok(training.pages.some((p) => p[0] === "planning"), "planning page");
assert.ok(!training.pages.some((p) => p[0] === "plan" || p[0] === "training"), "old pages gone");
assert.equal(REDIRECTS.plan("?week=2026-11-02"), "planning?week=2026-11-02", "plan redirect");
assert.equal(REDIRECTS.training("?period=last_3m"), "planning/analysis?period=last_3m", "training redirect");

assert.equal(REDIRECTS.today(""), "dashboard");
assert.equal(REDIRECTS.day("", ["2026-10-30"]), "dashboard/2026-10-30");
assert.equal(REDIRECTS.diary("?period=last_90d"), "dashboard?period=last_90d&section=diary");
assert.equal(REDIRECTS.calendar("?m=sleep.score"), "dashboard?m=sleep.score&section=calendar");
assert.equal(REDIRECTS.data(""), "settings/data/sync");
assert.equal(REDIRECTS.sources("?x=1"), "settings/data/sources?x=1");
assert.equal(REDIRECTS.history(""), "settings/data/history");
assert.equal(PARENT.diary, undefined);

function mockNav() {
  const pages = SECTIONS.flatMap((s) => s.pages.map((p) => ({ section: s.id, id: p[0] })));
  const links = pages.map((p) => ({
    dataset: { page: p.id },
    attrs: {},
    setAttribute(k, v) {
      this.attrs[k] = v;
    },
    removeAttribute(k) {
      delete this.attrs[k];
    },
    getAttribute(k) {
      return this.attrs[k];
    },
  }));
  const sections = SECTIONS.map((s) => ({
    dataset: { section: s.id },
    classList: { list: new Set(), toggle(c, on) { if (on) this.list.add(c); else this.list.delete(c); } },
    querySelector(sel) {
      const m = sel.match(/a\[data-page="([^"]+)"\]/);
      return m && links.some((l) => l.dataset.page === m[1]) ? { dataset: { page: m[1] } } : null;
    },
  }));
  return {
    querySelectorAll(sel) {
      if (sel === "a[data-page]") return links;
      if (sel === "[data-section]") return sections;
      return [];
    },
    current() {
      return links.find((l) => l.attrs["aria-current"] === "page")?.dataset.page;
    },
  };
}

for (const [route, expected] of [
  ["dashboard", "dashboard"],
  ["planning", "planning"],
  ["activity", "activities"],
]) {
  const nav = mockNav();
  markCurrent(nav, route);
  assert.equal(nav.current(), expected, route);
}

const legacyNav = {
  sections: ["data", "training", "today", "health"],
  pages: {
    data: ["sources", "data", "history"],
    training: ["activities", "calendar"],
    health: ["diary", "sleep"],
    today: ["dashboard", "today"],
  },
  hidden: [],
};
const laidOut = layout(legacyNav);
assert.ok(!laidOut.some((s) => s.id === "data"), "removed data section ignored");
const daySection = laidOut.find((s) => s.id === "today");
assert.ok(daySection, "today section");
assert.deepEqual(
  daySection.pages.map((p) => p.id),
  ["dashboard"],
  "saved prefs naming the old Day pages leave only the start page",
);
const training2 = laidOut.find((s) => s.id === "training");
assert.ok(!training2.pages.some((p) => p.id === "calendar"), "old calendar entry is gone");

console.log("check_nav: ok");
