// The session purpose on the activity page: the line under the choice, the automatic race or
// test mark with its undo, and the thresholds a marked race or test gave (#84).
import { clock, esc } from "./format.js";
import { N, t } from "./i18n.js";

export function purposeLabel(value) {
  return ({
    training: "Training",
    recovery: "Recovery",
    leisure: "Leisure",
    coached_by_me: "Coached by me",
    race: "Race",
    test: "Test",
  })[value] || value;
}

// What the purpose line says: chosen, marked by itself (#84), undone, or not set.
export function purposeText(purpose, mark = {}) {
  if (mark.automatic && purpose === "race") return t("Marked as a race by itself: you had a race that day. Not right? Undo it below.");
  if (mark.automatic) return t("Marked as a test by itself: a test was planned that day. Not right? Undo it below.");
  if (purpose) return t("Saved purpose: {purpose}.", { purpose: t(purposeLabel(purpose)) });
  if (mark.undone) return t("Not a race or test: it won't be marked again.");
  return t("No purpose tag yet; threshold updates skip this session until you tag it.");
}

const MISSING = {
  no_power: N("there is no power data"),
  too_short: N("it is shorter than the test"),
  no_laps: N("it has no laps"),
  no_test_laps: N("no lap has the length of the test"),
  no_test_lap: N("no lap has the length of the test"),
  no_heart_rate: N("there is no heart rate"),
  same_as_current: N("it matches your current values"),
};

// After saving a race or test: new proposals, or why there is none.
export function thresholdOutcome(result) {
  if (!result) return "";
  if (result.withdrawn?.length) return t("Its threshold proposals are withdrawn.");
  if (result.proposed?.length) return t("New thresholds wait for you under Zones & thresholds.");
  if (!("values" in result) || result.values?.length) return "";
  const reason = (result.missing || []).map((m) => MISSING[m.reason]).find(Boolean);
  return reason ? t("No new threshold: {reason}.", { reason: t(reason) }) : "";
}

const VALUE_NAMES = {
  ftp: N("FTP"),
  css: N("CSS"),
  lthr: N("Threshold heart rate"),
  threshold_pace: N("Threshold pace"),
  max_hr: N("Max heart rate"),
};
const STATUS_NAMES = {
  proposed: N("waiting for you"),
  current: N("in use"),
  superseded: N("replaced"),
  dismissed: N("ignored"),
};

// The thresholds this race or test gave, each with where it stands, and the way to them.
export function markCardHtml(a) {
  const rows = a.threshold_values || [];
  if (!rows.length) return "";
  const items = rows.map((r) => `<li>${esc(t(VALUE_NAMES[r.metric] || r.metric))}: <strong>${esc(thresholdValue(r))}</strong> <span class="sub">${esc(t(STATUS_NAMES[r.status] || r.status))}</span></li>`).join("");
  const title = a.purpose === "race" ? t("Thresholds from this race") : t("Thresholds from this test");
  return `<section class="card" aria-labelledby="activity-marks-title"><h2 id="activity-marks-title">${esc(title)}</h2>
    <ul>${items}</ul>
    <p><a href="#/zones">${esc(t("Zones & thresholds"))}</a></p></section>`;
}

function thresholdValue(row) {
  if (row.unit === "s/km") return `${clock(row.value)} /km`;
  if (row.unit === "s/100m") return `${clock(row.value)} /100 m`;
  return `${Math.round(row.value)} ${row.unit}`;
}
