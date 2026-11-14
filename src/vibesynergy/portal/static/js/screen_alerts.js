// The warnings that appear at the top of the screen, also after you closed them there:
// the same choice as the header (open warnings, and what happened in the last day), without
// the coach's daily note, which has its own place on the start page.
export function screenAlerts(data, now = Date.now()) {
  if (!data) return [];
  const dayAgo = now - 24 * 3600 * 1000;
  const seen = new Set();
  return [
    ...(data.open || []).filter((a) => a.level !== "info"),
    ...(data.recent || []).filter((e) => Date.parse(e.at) > dayAgo),
  ]
    .filter((a) => !String(a.key).startsWith("daily:") && a.kind !== "daily_coach" && a.text)
    .filter((a) => !seen.has(a.key) && seen.add(a.key))
    .map((a) => ({ kind: "screen_alert", text: a.text, link: a.link || "#/dashboard" }));
}
