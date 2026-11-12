// "Get started": the three setup steps with their live state, at the top of the start page
// while setup is incomplete. The state comes from the portal (/api/setup: a connected source,
// a threshold, a coach); putting the guide away is saved with the portal preferences, so it
// stays away on every device.
import { post } from "./api.js";
import { esc } from "./format.js";
import { N, t } from "./i18n.js";

const STEPS = [
  ["source", N("Connect a source"), N("Garmin, Strava or Withings bring in your training, sleep and weight."), "#/settings/data/sources"],
  ["zones", N("Set your zones and thresholds"), N("Your heart rate, power and pace zones: what an easy or a hard session means for you."), "#/zones"],
  ["coach", N("Set up your coach"), N("An AI coach reads your data and gives advice every day."), "#/coach/settings"],
];

/** The card, or "" when there is nothing to do: setup is complete, put away or unknown.
 * `open`: whether the steps show at once or wait behind the card's heading. */
export function setupGuideHtml(setup, { open = true } = {}) {
  if (!setup || setup.complete || setup.dismissed) return "";
  const done = STEPS.filter(([id]) => setup.steps[id]).length;
  const next = STEPS.find(([id]) => !setup.steps[id])?.[0];
  const rows = STEPS.map(([id, title, why, href]) => {
    const ok = Boolean(setup.steps[id]);
    // One main action in the card: the first step still to do.
    return `<li class="setup-step${ok ? " is-done" : ""}">
      <span class="chip ${ok ? "success" : "planned"}">${esc(ok ? t("Done") : t("To do"))}</span>
      <span class="setup-step-text"><strong>${esc(t(title))}</strong><span class="sub">${esc(t(why))}</span></span>
      <a class="btn${id === next ? " primary" : ""}" href="${href}" aria-label="${esc(ok ? t("Open: {step}", { step: t(title) }) : t(title))}">${esc(ok ? t("Open") : t("Set up"))}</a>
    </li>`;
  }).join("");
  // A disclosure: open on a wide screen, closed on a phone, where today's advice has to stay
  // on the first screen; the summary still says how far you are.
  return `<section class="card setup-guide" data-setup-guide aria-labelledby="setup-guide-title">
    <details class="collapsible"${open ? " open" : ""}>
      <summary><h2 id="setup-guide-title">${esc(t("Get started"))}</h2> <span class="sub">${esc(t("{done} of {total} done", { done, total: STEPS.length }))}</span></summary>
      <p class="sub">${esc(t("Three steps and VibeSynergy works for you."))}</p>
      <ol class="setup-steps">${rows}</ol>
      <div class="setup-guide-foot"><button class="btn" type="button" data-setup-dismiss>${esc(t("Hide this guide"))}</button>
        <span class="sub" data-setup-status role="status" aria-live="polite"></span></div>
    </details>
  </section>`;
}

/** Wire the card's "Hide this guide" button; `onDismiss` runs after it is put away. */
export function bindSetupGuide(view, onDismiss) {
  const card = view.querySelector("[data-setup-guide]");
  const button = card?.querySelector("[data-setup-dismiss]");
  if (!button) return;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await post("prefs", { changes: { setup_guide_dismissed: true } });
    } catch (error) {
      button.disabled = false;
      card.querySelector("[data-setup-status]").textContent = t("Not saved: {error}", { error: error.message });
      return;
    }
    card.remove();
    // Focus goes to the page's heading, not nowhere.
    const heading = view.querySelector("h1");
    if (heading) {
      heading.tabIndex = -1;
      heading.focus();
    }
    onDismiss?.();
  });
}
