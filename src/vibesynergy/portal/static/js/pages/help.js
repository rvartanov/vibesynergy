// Help: short video manuals, one per thing you do in VibeSynergy. Each video has Dutch and
// English captions; the one in your language is on. Videos load only when you play them.
// #/help/<id> opens the page at that manual.
import { esc } from "../format.js";
import { N, language, t } from "../i18n.js";

// [id, title, what it shows] per group. The files are static/help/<id>.webm (+ .jpg poster,
// .nl.vtt and .en.vtt captions); tests/test_help_manuals.py checks they are all there.
export const MANUALS = [
  [N("Getting started"), [
    ["login", N("Logging in and out"), N("Log in with your password, and where to log out.")],
    ["today", N("Your day: advice, recovery and check-in"), N("Read today's advice and recovery, and fill in the morning check-in.")],
    ["quicklog", N("Quick logging with +"), N("Log a check-in, a note or a discomfort from any page.")],
    ["versions", N("Version and what changed"), N("See which version you have, read what changed after an update, and find it again later.")],
  ]],
  [N("Training"), [
    ["activities", N("Finding and reviewing activities"), N("Filter and search your workouts, open one and say how it felt.")],
    ["workout-texts", N("Your texts on a workout"), N("Change a workout's name and texts, go back to an earlier version, and publish it.")],
    ["workout-gear", N("Gear on a workout"), N("Link the gear you used with one tap, add swim gear of your own and match your gear with Garmin's.")],
    ["workout-photos", N("Photos on a workout"), N("Add photos to a workout, see the ones from Strava and Garmin, and remove or put one back.")],
    ["calendar", N("Calendar: looking back and ahead"), N("See per month or week what you did and what is planned.")],
    ["planning", N("Planning & progress"), N("Compare the plan with what you did, and see your training analysis.")],
    ["races", N("Races and events"), N("Add, change or delete a race or another event, such as a training camp or a holiday.")],
    ["plan-builder", N("Building a training plan"), N("Draft a plan towards a race from your hours and rest days.")],
    ["group-training", N("Coaching a group training with RunSynergy"), N("Connect RunSynergy and adopt the group schedule as the workout for your Training geven session.")],
    ["fitness", N("Zones, thresholds and form"), N("Change a threshold after a test, and follow your form and best efforts.")],
    ["coach", N("Setting up and using your AI coach"), N("Add your AI key, set your AI coach's tasks, and tell it what to remember.")],
    ["coach-methods", N("Methods & rules: the texts your AI coach follows"), N("See what changes, what VibeSynergy fixed itself, and undo a fix.")],
  ]],
  [N("Health"), [
    ["health", N("Recovery, sleep and body"), N("Follow HRV, sleep and weight over time.")],
    ["injuries", N("Tracking complaints and injuries"), N("Score a complaint every day and see whether it improves.")],
  ]],
  [N("Insight"), [
    ["insight", N("Metrics, correlations and SQL"), N("Chart metrics together, look for links, and ask your own questions.")],
    ["search-inbox", N("Search and 'Waiting for you'"), N("Find anything with /, and approve what waits for you.")],
  ]],
  [N("Settings"), [
    ["sources", N("Connecting and syncing sources"), N("Connect Garmin, Strava, Withings or intervals.icu, and import older data.")],
    ["sharing", N("Sharing with your coach or an app"), N("Make a share link or a token for an AI app.")],
    ["account", N("Securing your account"), N("Change your password, turn on two-step verification and choose alerts.")],
    ["customize", N("Making VibeSynergy your own"), N("Arrange the start page, pick a theme and hide pages from the menu.")],
  ]],
];

const asset = (name) => new URL(`../../help/${name}`, import.meta.url).href;

function manualHtml([id, title, about]) {
  const lang = language() === "nl" ? "nl" : "en";
  const track = (code, label) =>
    `<track kind="captions" srclang="${code}" label="${esc(label)}" src="${esc(asset(`${id}.${code}.vtt`))}"${code === lang ? " default" : ""}>`;
  return `<article class="card help-manual" id="help-${esc(id)}">
    <h3><a href="#/help/${esc(id)}">${esc(t(title))}</a></h3>
    <p class="sub">${esc(t(about))}</p>
    <video controls preload="none" playsinline poster="${esc(asset(`${id}.jpg`))}" aria-label="${esc(t("Video: {title}", { title: t(title) }))}">
      <source src="${esc(asset(`${id}.webm`))}" type="video/webm">
      ${track("nl", "Nederlands")}${track("en", "English")}
    </video>
  </article>`;
}

export async function render(view, { args = [] } = {}) {
  document.title = `${t("Help")} · VibeSynergy`;
  view.innerHTML = `<div class="page-head"><div><h1>${esc(t("Help"))}</h1>
      <p class="sub">${esc(t("Short videos that show how to do things in VibeSynergy. Captions are on; turn them off or switch language in the player."))}</p></div></div>
    <nav class="help-index" aria-label="${esc(t("Manuals"))}"><ul class="plain">${MANUALS.flatMap(([, items]) => items).map(([id, title]) => `<li><a href="#/help/${esc(id)}">${esc(t(title))}</a></li>`).join("")}</ul></nav>
    ${MANUALS.map(([group, items]) => `<section class="help-group"><h2>${esc(t(group))}</h2><div class="help-grid">${items.map(manualHtml).join("")}</div></section>`).join("")}`;
  if (args[0]) {
    const target = view.querySelector(`#help-${CSS.escape(args[0])}`);
    if (target) {
      target.scrollIntoView({ block: "start" });
      target.querySelector("h3 a")?.focus({ preventScroll: true });
    }
  }
}
