// The workout's own texts on its page (#114): the name, a public part and a private part,
// each with its history and Restore, and the Publish tick. Only on your own portal: a
// shared page never loads this (the API routes are not shareable).
//
// Public part: the AI coach's public insight and the statistics (what publishing puts in
// the description, read-only), then your own public text. Private part: the AI coach's
// comments (read-only), then your own private text. Each text saves when you leave the
// field or press Save; every change is a version you can go back to.
import { get, post } from "./api.js";
import { esc, userDateTime } from "./format.js";
import { N, t } from "./i18n.js";

const SOURCE = { vs: "VibeSynergy", strava: "Strava", garmin: "Garmin", ai: N("AI coach") };
const KIND = { seed: N("Taken over"), edit: N("Edited"), restore: N("Restored"), overwritten: N("Overwritten") };
const FIELD_LABEL = { name: N("Name"), public: N("Your public text"), private: N("Your private text") };

function sourceLabel(source) {
  return source === "ai" ? t(SOURCE.ai) : SOURCE[source] || source || "";
}

// What the tick does now, said plainly per destination.
function publishHelp(p) {
  if (p.state === "unavailable") return t("Connect a matching Garmin or Strava activity before publishing.");
  const what = p.destination === "garmin"
    ? t("Publishing adds the AI-coach insight and the statistics to the description on Garmin, below the text that is already there.")
    : p.mirror
      ? t("Publishing adds the AI-coach insight and the statistics to the description on Strava, below the text that is already there, and copies that description to Garmin.")
      : t("Publishing adds the AI-coach insight and the statistics to the description on Strava, below the text that is already there.");
  const texts = p.texts_synced
    ? t("Once published, the name and your public text are kept the same on Strava and Garmin. Your private part is never sent.")
    : t("The name and the texts you write here are not sent.");
  const hidden = p.strava_private
    ? ` ${t("On Strava only you can see this activity. VibeSynergy cannot make it public; change that on Strava.")}`
    : "";
  return `${what} ${texts}${hidden}`;
}

const STATE_TEXT = {
  published: N("Published."),
  automatic: N("New workouts are public, so this one is published by itself within about 15 minutes. Untick to keep it to yourself."),
  off: N("Only you: nothing of this workout is published."),
  busy: N("The last change on the platform did not finish. Tick or untick again to finish it."),
  unavailable: "",
};

function publishHtml(p) {
  const disabled = p.state === "unavailable";
  const choice = p.default === "public" ? t("public") : t("only me");
  return `<section class="wt-publish" aria-labelledby="wt-publish-title">
    <h3 id="wt-publish-title">${t("Publish")}</h3>
    <label class="check"><input type="checkbox" data-wt-publish aria-describedby="wt-publish-help wt-publish-state"${p.checked ? " checked" : ""}${disabled ? " disabled" : ""}> ${t("Publish this workout")}</label>
    <p class="sub" id="wt-publish-help">${esc(publishHelp(p))}</p>
    <p class="sub" id="wt-publish-state">${esc(t(STATE_TEXT[p.state] || ""))}
      ${esc(t("New workouts: {choice}.", { choice }))} <a href="#/settings/sync">${t("Change this in Settings")}</a></p>
    <p class="sub" role="status" data-wt-publish-status></p>
  </section>`;
}

function historyHtml(field, versions, tz) {
  const mine = versions.filter((v) => v.field === field);
  if (!mine.length) {
    return `<details class="wt-history"><summary>${t("History")}</summary><p class="sub">${t("No earlier versions yet. Your first change keeps what was there.")}</p></details>`;
  }
  // A version that lost to a later change (the sync) is never the current one.
  const latest = mine.find((v) => v.kind !== "overwritten") || mine[0];
  const current = latest.version;
  const rows = mine.map((v) => {
    const when = userDateTime(v.at, tz);
    const text = v.text ? esc(v.text) : `<span class="muted">${esc(t("(empty)"))}</span>`;
    // A version with the current text needs no Restore: it would change nothing.
    const action = v.version === current
      ? `<span class="chip good">${t("Current")}</span>`
      : v.text === latest.text ? "" : `<button class="btn" type="button" data-wt-restore="${esc(v.version)}" aria-label="${esc(t("Restore the version of {when}", { when }))}">${t("Restore")}</button>`;
    const label = v.kind === "overwritten"
      ? t("Overwritten, from {source}", { source: sourceLabel(v.source) })
      : `${sourceLabel(v.source)} · ${t(KIND[v.kind] || v.kind)}`;
    return `<li><div class="sub">${esc(label)} · <time datetime="${esc(v.at)}">${esc(when)}</time></div>
      <div class="wt-version-text">${text}</div>${action}</li>`;
  }).join("");
  return `<details class="wt-history"><summary>${esc(t("History ({n})", { n: mine.length }))}</summary><ol class="wt-versions">${rows}</ol></details>`;
}

function textField(field, current, limit, versions, tz) {
  const id = `wt-${field}`;
  const from = !current.stored && current.text && current.source
    ? `<p class="sub">${esc(t("Taken from {source}.", { source: sourceLabel(current.source) }))}</p>`
    : "";
  const input = field === "name"
    ? `<input class="btn" type="text" id="${id}" name="${field}" maxlength="${limit}" value="${esc(current.text)}" autocomplete="off">`
    : `<textarea class="btn" id="${id}" name="${field}" rows="4" maxlength="${limit}">${esc(current.text)}</textarea>`;
  return `<div class="wt-field" data-wt-field="${field}">
    <label for="${id}">${t(FIELD_LABEL[field])}</label>${input}${from}
    <div class="ql-actions"><button class="btn" type="button" data-wt-save>${t("Save")}</button></div>
    <p class="sub" role="status" data-wt-status></p>
    ${historyHtml(field, versions, tz)}
  </div>`;
}

function coachCommentsHtml(comments, tz) {
  if (!comments.length) return `<p class="sub">${t("No comments from your AI coach yet.")}</p>`;
  return `<ul class="wt-coach">${comments.map((c) => `<li><div class="sub"><time datetime="${esc(c.at)}">${esc(userDateTime(c.at, tz))}</time></div><div class="wt-version-text">${esc(c.text)}</div></li>`).join("")}</ul>`;
}

export function workoutHtml(data, tz) {
  const { texts, history, limits } = data;
  const insight = data.public.insight?.state === "present" && data.public.insight.text
    ? `<p class="assessment-summary">${esc(data.public.insight.text)}</p>`
    : `<p class="sub">${t("No public insight from your AI coach yet.")}</p>`;
  const stats = data.public.stats.length
    ? `<pre class="publication-description" tabindex="0" aria-label="${esc(t("Statistics"))}">${esc(data.public.stats.join("\n"))}</pre>`
    : `<p class="sub">${t("No figures to publish yet.")}</p>`;
  return `<h2 id="wt-title">${t("Name and texts")}</h2>
    ${textField("name", texts.name, limits.name, history, tz)}
    <section class="wt-part wt-public" aria-labelledby="wt-public-title">
      <h3 id="wt-public-title"><span class="chip info">${t("Public")}</span> ${t("Public part")}</h3>
      <p class="sub">${t("Meant for others. Publishing puts the AI-coach insight and the statistics in the description.")}</p>
      <h4>${t("Insight from your AI coach")}</h4>${insight}
      <h4>${t("Statistics")}</h4>${stats}
      ${textField("public", texts.public, limits.text, history, tz)}
    </section>
    <section class="wt-part wt-private" aria-labelledby="wt-private-title">
      <h3 id="wt-private-title"><span class="chip">${t("Only you")}</span> ${t("Private part")}</h3>
      <p class="sub">${t("Only you see this part. It is never published and never shown on a shared page.")}</p>
      <h4>${t("Comments from your AI coach")}</h4>${coachCommentsHtml(data.private.coach, tz)}
      ${textField("private", texts.private, limits.text, history, tz)}
    </section>
    ${publishHtml(data.publish)}`;
}

// Mount the card into `el`; `onName(name)` follows a saved name (the page heading).
export async function workoutTexts(el, id, { stale, tz, onName }) {
  if (!el) return;
  let data;
  try {
    data = (await get("activity-texts", { id })).data;
  } catch (error) {
    if (!stale() && el.isConnected) el.innerHTML = `<p class="sub error-text">${esc(error.message)}</p>`;
    return;
  }
  if (stale() || !el.isConnected) return;
  const limitOf = (field) => (field === "name" ? data.limits.name : data.limits.text);

  // Redraw one field after a save or restore; the others keep what you are typing. Focus
  // stays on the control it was on (or `focus`), so the keyboard keeps its place.
  const redraw = (field, message, focus) => {
    const box = el.querySelector(`[data-wt-field=${field}]`);
    if (!box) return;
    const active = box.contains(document.activeElement)
      ? (document.activeElement.matches("[data-wt-save]") ? "[data-wt-save]" : `[name=${field}]`)
      : null;
    box.outerHTML = textField(field, data.texts[field], limitOf(field), data.history, tz);
    const fresh = el.querySelector(`[data-wt-field=${field}]`);
    bindField(fresh);
    fresh.querySelector("[data-wt-status]").textContent = message;
    const target = focus || active;
    if (target) fresh.querySelector(target)?.focus();
    if (field === "name" && data.texts.name.text) onName?.(data.texts.name.text);
  };

  const bindField = (box) => {
    const field = box.dataset.wtField;
    const input = box.querySelector(`[name=${field}]`);
    const status = box.querySelector("[data-wt-status]");
    let saved = input.value;
    let busy = null;
    const save = (asked) => {
      if (busy) return busy;
      const value = input.value;
      if (value === saved) {
        if (asked) status.textContent = t("Nothing changed.");
        return null;
      }
      status.classList.remove("error-text");
      status.textContent = t("Saving…");
      busy = (async () => {
        try {
          const reply = await post("activity-texts", { id, [field]: value });
          saved = value;
          if (stale() || !el.isConnected) return;
          data = { ...data, texts: reply.data.texts, history: reply.data.history };
          // Only redraw when nothing new was typed while saving.
          if (input.value === value) redraw(field, t("Saved."));
          else status.textContent = t("Saved.");
        } catch (error) {
          status.classList.add("error-text");
          status.textContent = t("Not saved: {error}", { error: error.message });
        } finally {
          busy = null;
        }
      })();
      return busy;
    };
    input.addEventListener("change", () => save(false));
    if (field === "name") {
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          save(true);
        }
      });
    }
    box.querySelector("[data-wt-save]").addEventListener("click", () => save(true));
    box.querySelectorAll("[data-wt-restore]").forEach((button) => button.addEventListener("click", async () => {
      if (button.getAttribute("aria-disabled") === "true") return;
      button.setAttribute("aria-disabled", "true");
      status.classList.remove("error-text");
      status.textContent = t("Saving…");
      try {
        const reply = await post("activity-texts/restore", { id, version: Number(button.dataset.wtRestore) });
        if (stale() || !el.isConnected) return;
        data = { ...data, texts: reply.data.texts, history: reply.data.history };
        redraw(field, t("Restored."), `[name=${field}]`);
      } catch (error) {
        button.removeAttribute("aria-disabled");
        status.classList.add("error-text");
        status.textContent = t("Not saved: {error}", { error: error.message });
      }
    }));
  };

  const bindPublish = () => {
    const box = el.querySelector("[data-wt-publish]");
    if (!box) return;
    const status = el.querySelector("[data-wt-publish-status]");
    let busy = false;
    box.addEventListener("click", (event) => { if (busy) event.preventDefault(); });
    box.addEventListener("change", async () => {
      const on = box.checked;
      const before = data.publish.state;
      busy = true;
      box.setAttribute("aria-busy", "true");
      status.classList.remove("error-text");
      status.textContent = on ? t("Publishing…") : t("Removing…");
      try {
        const reply = await post("activity-publish", { id, publish: on });
        if (stale() || !el.isConnected) return;
        data = { ...data, publish: reply.data.publish };
        const section = el.querySelector(".wt-publish");
        section.outerHTML = publishHtml(data.publish);
        bindPublish();
        el.querySelector("[data-wt-publish]")?.focus();
        const message = on
          ? t("Published.")
          : before === "automatic" || reply.data.result?.state === "unchanged"
            ? t("Kept to yourself: it is not published by itself.")
            : t("No longer published: the VibeSynergy section was removed.");
        el.querySelector("[data-wt-publish-status]").textContent = message;
      } catch (error) {
        box.checked = !on;
        status.classList.add("error-text");
        const known = {
          no_figures: t("This activity has no Garmin figures to publish yet."),
          no_destination: t("Connect a matching Garmin or Strava activity before publishing."),
        };
        status.textContent = t("Not published: {reason}", { reason: known[error.code] || error.message });
      } finally {
        busy = false;
        box.removeAttribute("aria-busy");
      }
    });
  };

  el.innerHTML = workoutHtml(data, tz);
  el.hidden = false;
  if (data.texts.name.stored && data.texts.name.text) onName?.(data.texts.name.text);
  el.querySelectorAll("[data-wt-field]").forEach(bindField);
  bindPublish();
}
