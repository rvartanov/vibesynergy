// Settings → Sync between sources (split from pages/settings.js): what VibeSynergy
// writes from one service to another (publication, calendars, agent links).
import { ago, esc, number } from "./format.js";
import { language, N, t } from "./i18n.js";
import { select } from "./settings_pieces.js";

// Where the quotes come from (8-11): name and who, all public domain, none religious.
const QUOTE_PROVIDERS = {
  builtin: [N("Built-in list"), N("127 quotes checked against their source")],
  wikiquote: [N("Wikiquote"), N("topic pages with author and work")],
  quotable: [N("Quotable"), N("free quote API with topics")],
  zenquotes: [N("ZenQuotes"), N("free quote API; shown with 'via zenquotes.io'")],
};
const QUOTE_SOURCES = {
  writers: [N("Writers and poets"), N("Shakespeare, Thoreau, Emerson, Whitman, Austen and others")],
  stoics: [N("Stoics"), N("Marcus Aurelius, Epictetus, Juvenal")],
  science: [N("Scientists"), N("Darwin, Faraday, Franklin, Adam Smith")],
  explorers: [N("Explorers and nature"), N("Shackleton, John Muir")],
  maxims: [N("Maxims"), N("La Rochefoucauld")],
};

// Settings → Sync between sources: what VibeSynergy writes from one service to another.
export function statusLine(tone, text) {
  return `<li><span class="chip ${tone}">${esc(tone === "good" ? t("OK") : t("Needs action"))}</span> ${text}</li>`;
}

export function publicationStatus(s) {
  const byId = Object.fromEntries(s.sources.map((src) => [src.id, src]));
  const strava = byId.strava?.login;
  const garmin = byId.garmin?.login;
  const pub = s.cross_sync?.publication || {};
  const lines = [];
  if (strava?.state !== "logged in") {
    lines.push(statusLine("warning", `${esc(t("Strava is not connected."))} <a href="#/settings/sources">${t("Connect in Sources")}</a>`));
  } else if (strava.write_access !== "granted") {
    lines.push(statusLine("warning", `${esc(t("Strava is connected, but VibeSynergy may not edit descriptions. Sign in to Strava again and allow uploads."))} <a href="#/settings/sources">${t("Sources")}</a>`));
  } else {
    lines.push(statusLine("good", esc(t("Strava is connected and VibeSynergy may edit descriptions."))));
  }
  if (garmin?.state === "logged in") lines.push(statusLine("good", esc(t("Garmin is connected."))));
  else lines.push(statusLine("warning", `${esc(t("Garmin is not connected, so there are no figures to publish."))} <a href="#/settings/sources">${t("Connect in Sources")}</a>`));
  const auto = s.publication?.auto || {};
  if (auto.enabled) lines.push(statusLine("good", esc(t("New activities are published by themselves."))));
  if (auto.needs_review) {
    lines.push(statusLine("warning", `${esc(t("{n} activities were not published. Open them and publish from their page.", { n: number(auto.needs_review) }))}${auto.problem ? ` <details><summary>${t("Details")}</summary><span class="sub">${esc(auto.problem)}</span></details>` : ""}`));
  }
  const last = pub.strava?.last_at
    ? t("Last published on Strava {when}.", { when: ago(pub.strava.last_at) })
    : t("Nothing has been published on Strava yet.");
  const problem = pub.problem || pub.conflicts
    ? `<p class="sub">${esc(t("The last copy to Garmin needs a look. Open the activity and preview it again."))}</p>
      ${pub.problem ? `<details><summary>${t("Details")}</summary><p class="sub">${esc(pub.problem)}</p></details>` : ""}`
    : "";
  return `<ul class="sync-status">${lines.join("")}</ul><p>${esc(last)}</p>${problem}`;
}

export function publicationOptions(s) {
  const pub = s.publication;
  const pubLabelDefaults = pub.defaults.labels[language()] || pub.defaults.labels.en;
  const lang = pub.language || pub.auto?.language || language();
  return `<div data-publication>
    <label class="check"><input type="checkbox" name="auto_publish" aria-describedby="sync-auto-help"${pub.auto_publish ? " checked" : ""}> ${t("Publish new activities by themselves")}</label>
    <p class="sub" id="sync-auto-help">${t("A new Garmin activity gets its figures on Strava within about 15 minutes. Activities from before you switch this on stay as they are.")}</p>
    <div class="settings-grid"><label>${t("Language of the text")} <select class="btn" name="publication_language">${[["nl", "Nederlands"], ["en", "English"]].map(([v, label]) => `<option value="${v}"${v === lang ? " selected" : ""}>${label}</option>`).join("")}</select></label></div>
    <label class="check"><input type="checkbox" name="garmin_mirror_enabled" aria-describedby="sync-mirror-help"${pub.garmin_mirror_enabled !== false ? " checked" : ""}> ${t("Copy the Strava text to Garmin too")}</label>
    <p class="sub" id="sync-mirror-help">${t("Garmin gets the same text and title as Strava, and follows your edits on Strava for 7 days. When off, publishing stays on Strava.")}</p>
    <label class="check"><input type="checkbox" name="icu_description_enabled" aria-describedby="sync-icu-help"${pub.icu_description_enabled !== false ? " checked" : ""}> ${t("Also in intervals.icu")}</label>
    <p class="sub" id="sync-icu-help">${t("intervals.icu gets the same block below its own description. Your text there stays as it is.")}</p>
    <details><summary>${t("What goes in the text")}</summary>
      <label class="check"><input type="checkbox" name="quote_enabled"${pub.quote_enabled ? " checked" : ""}> ${t("Add a sourced quote")}</label>
      <label class="check"><input type="checkbox" name="weather_enabled"${pub.weather_enabled ? " checked" : ""}> ${t("Add the weather during the activity")}</label>
      <fieldset class="settings-field"><legend>${t("Where quotes are fetched")}</legend>
        <p class="sub">${t("New quotes are fetched from the sources you switch on whenever a topic runs low. Only authors who died more than 70 years ago, nothing religious; every quote is used once.")}</p>
        ${pub.providers.map((p) => `<label class="check"><input type="checkbox" name="quote_provider" value="${esc(p)}"${pub.quote_providers.includes(p) ? " checked" : ""}> ${esc(t(QUOTE_PROVIDERS[p]?.[0] || p))} <span class="sub">(${esc(t(QUOTE_PROVIDERS[p]?.[1] || ""))}${p === "builtin" ? "" : `; ${t("{n} left", { n: (pub.quotes_left || {})[p] ?? 0 })}`})</span></label>`).join("")}
      </fieldset>
      <fieldset class="settings-field"><legend>${t("Built-in list")}</legend>
        <p class="sub">${t("Every quote is used only once. Switch off a source you don't want; the number is how many are not used yet.")}</p>
        ${pub.sources.map((source) => `<label class="check"><input type="checkbox" name="quote_source" value="${esc(source)}"${pub.quote_sources.includes(source) ? " checked" : ""}> ${esc(t(QUOTE_SOURCES[source]?.[0] || source))} <span class="sub">(${esc(t(QUOTE_SOURCES[source]?.[1] || ""))}; ${esc(t("{n} left", { n: (pub.quotes_left || {})[source] ?? 0 }))})</span></label>`).join("")}
      </fieldset>
      <details><summary>${t("Quote themes")}</summary><div class="settings-datasets">
        ${pub.themes.map((theme) => `<label class="check"><input type="checkbox" name="quote_theme" value="${esc(theme)}"${pub.quote_themes.includes(theme) ? " checked" : ""}> ${esc(t(theme))}</label>`).join("")}
      </div></details>
      <h3 class="settings-subhead">${t("Description block markers")}</h3>
      <p class="sub">${t("Opening and closing lines that wrap the published statistics block. Leave blank to use the default.")}</p>
      <div class="settings-grid">
        <label>${t("Opening line")}<input class="btn" type="text" name="opener" maxlength="100" placeholder="${esc(pub.defaults.opener)}" value="${esc(pub.opener === pub.defaults.opener ? "" : pub.opener)}"></label>
        <label>${t("Closing line")}<input class="btn" type="text" name="closer" maxlength="100" placeholder="${esc(pub.defaults.closer)}" value="${esc(pub.closer === pub.defaults.closer ? "" : pub.closer)}"></label>
      </div>
      <h3 class="settings-subhead">${t("Section names in the published block")}</h3>
      <p class="sub">${t("Optional names for the statistics and quote sections. Leave blank for the default in your language.")}</p>
      <div class="settings-grid">
        <label>${t("Statistics section name")}<input class="btn" type="text" name="label_stats" maxlength="40" placeholder="${esc(pubLabelDefaults.stats)}" value="${esc(pub.label_stats || "")}"></label>
        <label>${t("Quote section name")}<input class="btn" type="text" name="label_quote" maxlength="40" placeholder="${esc(pubLabelDefaults.quote)}" value="${esc(pub.label_quote || "")}"></label>
      </div>
    </details>
    <p class="sub" role="status" data-publication-status></p>
  </div>`;
}

export function calendarStatus(s) {
  const cal = s.cross_sync?.calendar || {};
  const parts = [];
  if (cal.copies) parts.push(t("{n} workouts are on your calendar.", { n: number(cal.copies) }));
  if (cal.last_run?.finished_at) parts.push(t("Last checked {when}.", { when: ago(cal.last_run.finished_at) }));
  if (cal.conflicts) parts.push(t("{n} copies need a look; VibeSynergy leaves them alone.", { n: number(cal.conflicts) }));
  return parts.length ? `<p>${esc(parts.join(" "))}</p>` : "";
}

export function syncTabHtml(s) {
  return `<section class="card" data-sync-publication>
      <h2>${t("Garmin → Strava")}</h2>
      <p class="sub">${t("VibeSynergy adds your Garmin figures to the activity description on Strava, below your own text. You can also publish one activity from its page.")}</p>
      ${publicationStatus(s)}
      ${publicationOptions(s)}
    </section>
    <section class="card">
      <h2>${t("AI coach after a workout")}</h2>
      <p class="sub">${t("The coach's links (webhooks) and running the coach on the API are under Coach → Settings.")} <a href="#/coach/settings">${t("Open the coach settings")}</a></p>
    </section>
    <section class="card" data-sync-calendar>
      <h2>${t("Garmin → calendar")}</h2>
      <p class="sub">${t("Copy your Garmin workouts onto your own calendar, such as iCloud. Your appointments stay untouched.")}</p>
      ${calendarStatus(s)}
      <div data-calendar-copy></div>
    </section>`;
}
