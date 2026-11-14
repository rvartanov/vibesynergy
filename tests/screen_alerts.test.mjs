import assert from "node:assert/strict";
import test from "node:test";

import { screenAlerts } from "../src/vibesynergy/portal/static/js/screen_alerts.js";

const NOW = Date.parse("2026-11-14T09:00:00Z");

test("the bell lists what the top of the screen shows, without the coach's daily note", () => {
  const out = screenAlerts(
    {
      open: [
        { key: "rhr", level: "warn", text: "Je rusthartslag ligt 5 bpm hoger.", link: "#/health" },
        { key: "info", level: "info", text: "Alleen info" },
        { key: "daily:2026-11-14", level: "warn", text: "Dagnotitie" },
      ],
      recent: [
        { key: "job1", kind: "job", text: "Sync mislukt", at: "2026-11-14T07:00:00Z" },
        { key: "old", kind: "job", text: "Gisteren eerder", at: "2026-11-13T07:00:00Z" },
        { key: "coach", kind: "daily_coach", text: "Coach", at: "2026-11-14T07:00:00Z" },
        { key: "rhr", level: "warn", text: "Je rusthartslag ligt 5 bpm hoger.", at: "2026-11-14T07:00:00Z" },
      ],
    },
    NOW,
  );
  assert.deepEqual(
    out.map((a) => [a.text, a.link]),
    [
      ["Je rusthartslag ligt 5 bpm hoger.", "#/health"],
      ["Sync mislukt", "#/dashboard"],
    ],
  );
});

test("no alerts answer, no lines", () => {
  assert.deepEqual(screenAlerts(null, NOW), []);
});
