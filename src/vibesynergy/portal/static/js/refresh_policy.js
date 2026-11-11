/** Whether an open data page should reload now.

`refreshMin` is the Appearance setting in minutes. 0 means automatic reload is off,
including across midnight. The page checks about every `pollMs`. Reloading one check
before the setting keeps the wait inside that setting instead of one check past it.
A calendar-day change reloads at once while automatic reload is on. A hidden document
waits until it is visible again.
*/
export function shouldAutoRefresh({
  now,
  renderedAt,
  refreshMin,
  hidden,
  pageAllowed,
  dayChanged,
  pollMs = 15_000,
}) {
  if (!pageAllowed || hidden || !refreshMin) return false;
  if (dayChanged) return true;
  const boundMs = refreshMin * 60_000;
  const waitMs = Math.max(0, boundMs - pollMs);
  return now - renderedAt >= waitMs;
}

/** Calendar day YYYY-MM-DD in an IANA time zone. */
export function calendarDay(timeZone, now) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const pick = (type) => parts.find((part) => part.type === type)?.value ?? "";
  return `${pick("year")}-${pick("month")}-${pick("day")}`;
}
