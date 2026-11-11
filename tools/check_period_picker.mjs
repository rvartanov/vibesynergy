import assert from "node:assert/strict";
import {
  computePickerValue,
  defaultYears,
  displayState,
  heatmapStorageValue,
  mergePeriodValue,
  periodFromQuery,
  queryHasPeriodKeys,
  resolvePeriod,
  validateCustomRange,
  writePeriodQuery,
} from "../src/vibesynergy/portal/static/js/period_picker.js";

const TODAY = "2026-10-03";
const monday = "2026-09-28";
const sunday = "2026-10-04";

assert.deepEqual(resolvePeriod({ period: "week" }, sunday), { start: monday, end: sunday });
assert.deepEqual(resolvePeriod({ period: "this_week" }, sunday), { start: monday, end: sunday });
assert.deepEqual(resolvePeriod({ period: "last_7d" }, TODAY), { start: "2026-09-27", end: TODAY });
assert.deepEqual(resolvePeriod({ period: "last_14d" }, TODAY), { start: "2026-09-20", end: TODAY });
assert.deepEqual(resolvePeriod({ period: "last_1m" }, "2026-03-31"), { start: "2026-03-01", end: "2026-03-31" });
assert.deepEqual(resolvePeriod({ period: "last_3m" }, "2026-05-31"), { start: "2026-03-01", end: "2026-05-31" });
assert.deepEqual(resolvePeriod({ period: "last_6m" }, "2026-08-31"), { start: "2026-03-01", end: "2026-08-31" });
assert.deepEqual(resolvePeriod({ period: "last_12m" }, TODAY), { start: "2025-10-04", end: TODAY });
// month clamp from 31st across year boundary
assert.deepEqual(resolvePeriod({ period: "last_3m" }, "2026-05-31").start, "2026-03-01");
assert.deepEqual(resolvePeriod({ period: "last_3m" }, "2026-01-31"), {
  start: "2025-11-01",
  end: "2026-01-31",
});
assert.deepEqual(resolvePeriod({ period: "next_7d" }, TODAY), { start: TODAY, end: "2026-10-09" });
assert.deepEqual(resolvePeriod({ period: "next_14d" }, TODAY), { start: TODAY, end: "2026-10-16" });
assert.deepEqual(resolvePeriod({ period: "next_1m" }, TODAY), { start: TODAY, end: "2026-11-02" });
assert.deepEqual(resolvePeriod({ period: "ytd" }, TODAY), { start: "2026-01-01", end: TODAY });
assert.deepEqual(resolvePeriod({ period: "2024" }, TODAY), { start: "2024-01-01", end: "2024-12-31" });
assert.deepEqual(resolvePeriod({ start: "2026-01-01", end: "2026-02-01" }, TODAY), {
  start: "2026-01-01",
  end: "2026-02-01",
});
assert.deepEqual(resolvePeriod({ start: "2026-01-01" }, TODAY), { start: "2026-01-01", end: TODAY });

const legacy = displayState({ period: "last_90d" }, TODAY, []);
assert.equal(legacy.mode, "custom");
assert.ok(legacy.start);

const ytd = displayState({ period: "this_year" }, TODAY, []);
assert.equal(ytd.period, "ytd");

const q = new URLSearchParams("year=2024");
assert.deepEqual(periodFromQuery(q, { heatmap: true, defaultValue: { period: "2026" }, today: TODAY }), {
  period: "2024",
});

const out = writePeriodQuery(new URLSearchParams(), { period: "last_7d" }, { defaultValue: { period: "last_7d" } });
assert.equal(out.toString(), "");

assert.ok(validateCustomRange("2026-01-02", "2026-01-01", {}));
assert.equal(validateCustomRange("2026-01-01", "2026-01-10", { maxDays: 5 }), "Choose at most 5 days.");
assert.equal(validateCustomRange("2026-01-01", "2027-01-01", {}), "");

const heat = writePeriodQuery(new URLSearchParams("m=x&period=last_3m"), { period: "next_7d" }, {
  defaultValue: { period: "next_14d" },
  omitDefault: false,
  param: { period: "agenda", start: "agenda_start", end: "agenda_end", legacyStart: "start", legacyEnd: "end" },
  today: TODAY,
});
assert.equal(heat.get("agenda"), "next_7d");
assert.equal(heat.get("period"), "last_3m");

const agendaKeys = {
  param: { period: "agenda", start: "agenda_start", end: "agenda_end", legacyStart: "start", legacyEnd: "end" },
};
assert.equal(queryHasPeriodKeys(new URLSearchParams("from=2026-01-01&to=2026-01-07"), { heatmap: true }), true);
assert.equal(queryHasPeriodKeys(new URLSearchParams("from=2026-01-01&to=2026-01-07"), agendaKeys), false);
assert.deepEqual(
  periodFromQuery(new URLSearchParams("start=2026-01-01"), { defaultValue: { period: "last_7d" }, today: TODAY }),
  { start: "2026-01-01", end: TODAY },
);
assert.deepEqual(
  periodFromQuery(new URLSearchParams("start=2026-11-01"), {
    defaultValue: { period: "next_14d" },
    today: TODAY,
    direction: "forward",
    ...agendaKeys,
  }),
  { start: "2026-11-01", end: "2026-11-14" },
);
assert.deepEqual(
  periodFromQuery(new URLSearchParams("period=custom"), { defaultValue: { period: "last_3m" }, today: TODAY }),
  { period: "last_3m" },
);
assert.deepEqual(
  mergePeriodValue(new URLSearchParams(), "vibesynergy.period.metrics", { period: "last_3m" }, { today: TODAY }),
  { period: "last_3m" },
);

const years = defaultYears(TODAY, { firstYear: 2020, selected: 2018 });
assert.ok(years.includes(2018));
assert.ok(years.includes(2026));
assert.ok(!years.some((y) => Number.isNaN(y)));
assert.ok(!defaultYears(TODAY, { selected: "last_3m" }).some((y) => Number.isNaN(y)));

assert.deepEqual(heatmapStorageValue({ period: "2026" }, TODAY), { period: "year" });
assert.deepEqual(heatmapStorageValue({ period: "2024" }, TODAY), { period: "2024" });

assert.deepEqual(
  computePickerValue(
    { start: "2026-01-01", end: "2026-01-07" },
    { start: "2026-02-01", end: "2026-02-10" },
    {},
    TODAY,
  ),
  { start: "2026-02-01", end: "2026-02-10" },
);
assert.deepEqual(
  computePickerValue({ period: "week" }, null, { max: TODAY }, TODAY),
  { start: monday, end: TODAY },
);

// Week spanning New Year
assert.deepEqual(resolvePeriod({ period: "week" }, "2026-01-01"), { start: "2025-12-29", end: "2026-01-04" });

console.log("period_picker resolve, URL round-trips and validation passed");
