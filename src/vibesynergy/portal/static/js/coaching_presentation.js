const IN_FLIGHT = new Set(["pending", "running", "generated", "waiting_inputs"]);

/** How to show a saved review so an older one is not the current advice.

`archive` is the record of a past day and may lead. `earlier` still has text, but newer
data or a failed current version means that text is not today's advice. `current` matched
the inputs just read. `status` has no saved text.
*/
export function savedAdviceRole(entry) {
  if (!entry?.advice) return "status";
  if (entry.display?.archive) return "archive";
  if (entry.advice.current === false || entry.state === "stale" || entry.state === "superseded") {
    return "earlier";
  }
  return "current";
}

/** Which status sentence leads when the saved text is earlier advice. */
export function earlierAdviceStatus(entry) {
  if (IN_FLIGHT.has(entry?.state)) return "preparing";
  if (entry?.state === "failed" || entry?.state === "interrupted") return "failed";
  return "outdated";
}
