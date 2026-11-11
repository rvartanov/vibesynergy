import { get, post } from "./api.js";
import { esc } from "./format.js";
import { N, language, t } from "./i18n.js";

const PLUGIN_NAME = {
  insight: N("Personal insight"),
  weather: N("Weather during the activity"),
  quote: N("Quote"),
};
const PLUGIN_STATUS = {
  ok: N("Included in the description"),
  disabled: N("Switched off"),
  unavailable: N("Left out this time"),
  failed: N("Could not be checked"),
  withheld: N("Left out"),
};

function blockedMessage(data) {
  if (data.reason === "no_public_statistics") return t("This activity has no Garmin figures to publish yet.");
  return t("This activity has no Garmin figures to publish yet.");
}

function publishResult(destination, data) {
  const mirror = data?.mirror;
  if (destination === "strava" && mirror?.error) return t("Published on Strava. Garmin will be tried again.");
  if (destination === "strava" && mirror?.state === "watching") return t("Published on Strava and copied to Garmin.");
  if (destination === "strava" && mirror?.state === "disabled") return t("Published on Strava.");
  return t("Published and checked on the connected service.");
}

function removeResult(destination, data) {
  const garmin = data?.mirror?.garmin;
  if (destination === "strava" && garmin === "failed") return t("Removed on Strava. Garmin still needs another try.");
  if (destination === "strava" && garmin === "removed") return t("Removed on Strava and on Garmin.");
  return t("VibeSynergy section removed and checked.");
}

export function annotationPreview(el, id, stale, destinations = []) {
  if (!el) return;
  el.innerHTML = `<details><summary>${t("Public annotation preview")}</summary>
    <p>${t("Review the full description before publishing it.")}</p>
    <button class="btn" type="button" data-preview>${t("Preview statistics block")}</button>
    <div data-result aria-live="polite"></div>
    ${destinations.length ? `<label>${t("Publish to")}
      <select class="btn" data-destination>${destinations.map((destination) => `<option value="${destination}">${destination === "garmin" ? "Garmin" : "Strava"}</option>`).join("")}</select></label>
    <button class="btn" type="button" data-full-preview>${t("Preview full description")}</button>` : `<p>${t("Connect a matching Garmin or Strava activity before publishing.")}</p>`}
    <div data-full-result aria-live="polite"></div></details>`;
  // Capture phase: stop Enter/Space before <details> sees the key and toggles shut.
  const keepOpen = (event) => {
    const button = event.target.closest?.("button");
    if (!button || !el.contains(button) || (event.key !== "Enter" && event.key !== " ")) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.type === "keydown" && !event.repeat) button.click();
  };
  el.addEventListener("keydown", keepOpen, true);
  el.addEventListener("keyup", keepOpen, true);
  el.querySelector("[data-preview]").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const output = el.querySelector("[data-result]");
    if (button.getAttribute("aria-disabled") === "true") return;
    // Keep the button focusable. Disabling it on keydown moves focus to the summary,
    // and the matching keyup then closes the disclosure.
    button.setAttribute("aria-disabled", "true");
    try {
      const {data} = await get("activity-annotation-preview", {id});
      if (stale() || !el.isConnected) return;
      output.innerHTML = data.state === "preview"
        ? `<p class="assessment-summary">${esc(data.block)}</p><p class="sub">${t("These are the Garmin figures recorded for this activity. Your private assessment stays private. Quote, weather and the short insight are in the full description when you turn them on.")}</p>`
        : `<p>${t("A current saved assessment with public statistics is required before preview.")}</p>`;
    } catch (error) {
      if (!stale() && el.isConnected) output.textContent = error.message;
    } finally {
      button.removeAttribute("aria-disabled");
      if (el.isConnected && document.activeElement === document.body) button.focus({preventScroll:true});
    }
  });
  el.querySelector("[data-full-preview]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const output = el.querySelector("[data-full-result]");
    const destination = el.querySelector("[data-destination]").value;
    if (button.getAttribute("aria-disabled") === "true") return;
    button.setAttribute("aria-disabled", "true");
    output.textContent = t("Loading…");
    try {
      const {data} = await get("activity-publication-preview", {id, destination, lang: language()});
      if (stale() || !el.isConnected) return;
      if (data.state !== "ready" && data.state !== "undo_pending") {
        output.textContent = blockedMessage(data);
        return;
      }
      output.innerHTML = `<p>${data.state === "undo_pending" ? t("Finish removing the VibeSynergy section.") : data.already_published ? t("Already published.") : t("Ready to publish. Review the complete text below.")}</p>
        <pre class="publication-description" tabindex="0" aria-label="${esc(t("Full description to publish"))}">${esc(data.description)}</pre>
        <details><summary>${t("Optional sections")}</summary><ul>
          ${Object.entries(data.plugins || {}).map(([name, state]) => `<li>${esc(t(PLUGIN_NAME[name] || name))}: ${esc(t(PLUGIN_STATUS[state.status] || state.status))}${state.reused ? ` — ${t("Previous checked text kept")}` : ""}</li>`).join("")}
        </ul></details>
        ${data.undo_description !== null && data.undo_description !== undefined ? `<details><summary>${t("Text after removal")}</summary><pre class="publication-description" tabindex="0" aria-label="${esc(t("Text after removal"))}">${esc(data.undo_description)}</pre></details><button class="btn" type="button" data-remove>${t(data.state === "undo_pending" ? "Finish removal" : "Remove VibeSynergy section")}</button>` : ""}
        ${data.state === "ready" && !data.already_published ? `<button class="btn primary" type="button" data-publish>${t("Publish description")}</button>` : ""}
        <p role="status" data-publish-status></p>`;
      output.querySelector("[data-remove]")?.addEventListener("click", async (click) => {
        const removeButton = click.currentTarget;
        if (removeButton.getAttribute("aria-disabled") === "true") return;
        removeButton.setAttribute("aria-disabled", "true");
        const status = output.querySelector("[data-publish-status]");
        status.textContent = t("Removing…");
        try {
          const removed = await post("activity-publication/remove", {id, destination, preview_version: data.preview_version, language: language()});
          status.textContent = removeResult(destination, removed.data);
        } catch (error) {
          status.textContent = error.message;
          removeButton.removeAttribute("aria-disabled");
        }
      });
      output.querySelector("[data-publish]")?.addEventListener("click", async (click) => {
        const publishButton = click.currentTarget;
        if (publishButton.getAttribute("aria-disabled") === "true") return;
        publishButton.setAttribute("aria-disabled", "true");
        const status = output.querySelector("[data-publish-status]");
        status.textContent = t("Publishing…");
        try {
          const published = await post("activity-publication", {id, destination, preview_version: data.preview_version, language: language()});
          status.textContent = publishResult(destination, published.data);
        } catch (error) {
          status.textContent = error.message;
          publishButton.removeAttribute("aria-disabled");
        }
      });
    } catch (error) { if (!stale() && el.isConnected) output.textContent = error.message; }
    finally {
      button.removeAttribute("aria-disabled");
      if (el.isConnected && document.activeElement === document.body) button.focus({preventScroll: true});
    }
  });
}
