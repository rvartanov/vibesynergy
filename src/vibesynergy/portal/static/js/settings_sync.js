// Settings → Publishing and sync (split from pages/settings.js): what VibeSynergy
// writes from one service to another (publishing workouts, calendars).
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

// Settings → Publishing and sync: what VibeSynergy writes from one service to another.
export function statusLine(tone, text) {
  return `<li><span class="chip ${tone}">${esc(tone === "good" ? t("OK") : t("Needs action"))}</span> ${text}</li>`;
}

const PLATFORM = { strava: "Strava", garmin: "Garmin" };

// One line per place under "Publish to" (#175): connected, may write, last synced and
// the workouts that need a look, with a link to the first one.
function targetLine(platform, login, pub) {
  const sources = `<a href="#/settings/sources">${t("Connect in Sources")}</a>`;
  if (login?.state !== "logged in") {
    const text = platform === "strava" ? t("Strava is not connected.") : t("Garmin is not connected.");
    return statusLine("warning", `${esc(text)} ${sources}`);
  }
  if (platform === "strava" && login.write_access !== "granted") {
    return statusLine("warning", `${esc(t("Strava is connected, but VibeSynergy may not edit descriptions. Sign in to Strava again and allow uploads."))} <a href="#/settings/sources">${t("Sources")}</a>`);
  }
  const ok = platform === "strava"
    ? t("Strava is connected and VibeSynergy may edit descriptions.")
    : t("Garmin is connected and VibeSynergy may edit descriptions.");
  const last = pub.last_at
    ? t("Last synced {when}.", { when: ago(pub.last_at) })
    : t("Nothing published here yet.");
  if (!pub.needs_review) return statusLine("good", `${esc(ok)} ${esc(last)}`);
  const look = pub.needs_review === 1
    ? t("1 workout needs a look.")
    : t("{n} workouts need a look.", { n: number(pub.needs_review) });
  const link = pub.review_activity
    ? ` <a href="#/activity/${encodeURIComponent(pub.review_activity)}">${t("Open the workout")}</a>`
    : "";
  return statusLine("warning", `${esc(ok)} ${esc(last)} ${esc(look)}${link}`);
}

export function publicationStatus(s) {
  const byId = Object.fromEntries(s.sources.map((src) => [src.id, src]));
  const pub = s.cross_sync?.publication || {};
  const targets = s.publication?.targets || ["strava", "garmin"];
  const lines = targets.map((platform) => targetLine(platform, byId[platform]?.login, pub[platform] || {}));
  // The figures come from Garmin even when nothing is published there.
  if (!targets.includes("garmin") && byId.garmin?.login?.state !== "logged in") {
    lines.push(statusLine("warning", `${esc(t("Garmin is not connected, so there are no figures to publish."))} <a href="#/settings/sources">${t("Connect in Sources")}</a>`));
  }
  return `<ul class="sync-status">${lines.join("")}</ul>`;
}

export function publicationOptions(s) {
  const pub = s.publication;
  const pubLabelDefaults = pub.defaults.labels[language()] || pub.defaults.labels.en;
  const lang = pub.language || pub.auto?.language || language();
  const targets = pub.targets || ["strava", "garmin"];
  return `<div data-publication>
    <fieldset class="settings-field" aria-describedby="sync-auto-help"><legend>${t("New workouts")}</legend>
      <label class="check"><input type="radio" name="new_workouts" value="public"${pub.auto_publish ? " checked" : ""}> ${t("Public: published by themselves")}</label>
      <label class="check"><input type="radio" name="new_workouts" value="only_me"${pub.auto_publish ? "" : " checked"}> ${t("Only me: you publish them yourself")}</label>
      <p class="sub" id="sync-auto-help">${t("Public: a new workout is published within about 15 minutes, and the Publish tick on the workout starts on. Only me: nothing is published until you tick Publish on the workout. Who can see the activity on Strava itself is still set on Strava. Workouts from before you choose Public stay as they are.")}</p>
    </fieldset>
    <fieldset class="settings-field" aria-describedby="sync-targets-help"><legend>${t("Publish to")}</legend>
      ${["strava", "garmin"].map((p) => `<label class="check"><input type="checkbox" name="publish_target" value="${p}"${targets.includes(p) ? " checked" : ""}> ${PLATFORM[p]}</label>`).join("")}
      <p class="sub" id="sync-targets-help">${t("A published workout gets VibeSynergy's section on each place you tick, below the text that is already there.")}</p>
    </fieldset>
    <label class="check"><input type="checkbox" name="text_sync_enabled" aria-describedby="sync-texts-help"${pub.text_sync_enabled ? " checked" : ""}> ${t("Keep name, texts and gear the same everywhere")}</label>
    <p class="sub" id="sync-texts-help">${t("A published workout gets the same name, public text and gear in VibeSynergy and on the places you publish to. Change it in one place and the others follow within a few minutes; every version stays in the workout's history. Strava takes one bike or pair of shoes, Garmin the gear you matched on the Gear page; swim gear such as a pull buoy stays in VibeSynergy. Your private text is never sent. Who can see the activity on Strava stays set on Strava.")} <a href="#/gear">${t("Match your gear")}</a></p>
    <label class="check"><input type="checkbox" name="auto_name_enabled" aria-describedby="sync-names-help"${pub.auto_name_enabled !== false ? " checked" : ""}> ${t("Give workouts a name automatically")}</label>
    <p class="sub" id="sync-names-help">${t("A new workout gets the title of its plan session, or else its sport, part of the day and place, such as \"Morning run Leeuwarden\", in the language of the text. Change a name yourself, here or on Strava or Garmin, and it stays: VibeSynergy no longer names that workout. Workouts from before this setting keep their name.")}</p>
    <details><summary>${t("What goes in the text")}</summary>
      <div class="settings-grid"><label>${t("Language of the text")} <select class="btn" name="publication_language">${[["nl", "Nederlands"], ["en", "English"]].map(([v, label]) => `<option value="${v}"${v === lang ? " selected" : ""}>${label}</option>`).join("")}</select></label></div>
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
      <h2>${t("Publish workouts")}</h2>
      <p class="sub">${t("Your workouts are VibeSynergy's. When you publish one, VibeSynergy keeps its name, public text, statistics and gear the same on Strava and Garmin.")}</p>
      ${publicationStatus(s)}
      ${publicationOptions(s)}
    </section>
    <section class="card" data-sync-calendar>
      <h2>${t("Garmin → calendar")}</h2>
      <p class="sub">${t("Copy your Garmin workouts onto your own calendar, such as iCloud. Your appointments stay untouched.")}</p>
      ${calendarStatus(s)}
      <div data-calendar-copy></div>
    </section>`;
}
