import assert from "node:assert/strict";
import test from "node:test";

globalThis.navigator ??= { language: "en" };
globalThis.localStorage ??= { getItem: () => null, setItem: () => {}, removeItem: () => {} };

const { historyHtml, newsCardHtml } = await import("../src/vibesynergy/portal/static/js/release_news.js");

// What changed (#140): the card once after a release, then every version under Notices.
const NEWS = {
  version: "0.1.0-beta.0.3",
  releases: [
    { version: "0.1.0-beta.0.3", date: "2026-12-03", items: [{ id: "2-you", audience: "you", text: "Item <b>for</b> you." }] },
    { version: "0.1.0-beta.0.1", date: "2026-12-01", items: [{ id: "1-old", audience: "you", text: "Old item." }] },
  ],
};

test("the card names the newest version and lists every unseen item", () => {
  const html = newsCardHtml(NEWS);
  assert.match(html, /What changed in version 0\.1\.0-beta\.0\.3/);
  assert.match(html, /Item &lt;b&gt;for&lt;\/b&gt; you\./);
  assert.match(html, /Old item\./);
  assert.match(html, /href="#\/inbox"/);
  assert.match(html, /data-release-news-close/);
});

test("no card without news", () => {
  assert.equal(newsCardHtml({ version: "0.1.0-beta.0.3", releases: [] }), "");
  assert.equal(newsCardHtml(null), "");
});

test("the history under Notices has every version, the newest open, dates as d-m-yyyy", () => {
  const html = historyHtml(NEWS.releases);
  assert.equal((html.match(/<details/g) || []).length, 2);
  assert.match(html, /<details class="details" open data-release="0\.1\.0-beta\.0\.3">/);
  assert.match(html, /3-12-2026/);
  assert.equal(historyHtml([]), "");
});
