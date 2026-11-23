// The workout's own texts on its page (#114): the name, a public part and a private part,
// each with its history and Restore, and the Publish tick. Only on your own portal: a
// shared page never loads this (the API routes are not shareable).
//
// Public part: the AI coach's public insight and the statistics (what publishing puts in
// the description, read-only), then your own public text. Private part: the AI coach's
// comments (read-only), then your own private text. Each text saves when you leave the
// field or press Save; every change is a version you can go back to.
//
// The insight follows your texts (#114): when a text changes after it, your AI coach writes
// it again; you can also correct it here (private). The page says when it is being written
// and keeps every earlier insight.
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

// Whether the insight is being written (again), in words.
function rewriteText(r) {
  switch (r?.state) {
    case "writing": return t("Your AI coach is writing the insight.");
    case "rewriting": return t("Your AI coach is rewriting the insight.");
    case "waiting": return t("Your text changed after this insight. Your AI coach rewrites it in a few minutes.");
    case "limit": return t("Your text changed after this insight. Your AI coach rewrites it tomorrow: today's rewrites are used up.");
    case "off": return t("Your text changed after this insight. Switch on your AI coach to have it rewritten.");
    default: return "";
  }
}

// Still on its way: the page looks again in a while.
export function rewritePending(r) {
  return ["writing", "rewriting", "waiting"].includes(r?.state);
}

function insightVersionsHtml(versions, tz) {
  const earlier = (versions || []).slice(1);
  if (!earlier.length) return "";
  const rows = earlier.map((v) => {
    const text = v.text ? esc(v.text) : `<span class="muted">${esc(t("(withdrawn)"))}</span>`;
    return `<li><div class="sub">${esc(t(SOURCE.ai))} · <time datetime="${esc(v.at)}">${esc(userDateTime(v.at, tz))}</time></div><div class="wt-version-text">${text}</div></li>`;
  }).join("");
  return `<details class="wt-history"><summary>${esc(t("Earlier insights ({n})", { n: earlier.length }))}</summary><ol class="wt-versions">${rows}</ol></details>`;
}

function correctionsHtml(corrections, tz) {
  if (!corrections?.length) return "";
  const rows = corrections.map((v) => `<li><div class="sub"><time datetime="${esc(v.at)}">${esc(userDateTime(v.at, tz))}</time></div><div class="wt-version-text">${esc(v.text)}</div></li>`).join("");
  return `<details class="wt-history"><summary>${esc(t("Your corrections ({n})", { n: corrections.length }))}</summary><ol class="wt-versions">${rows}</ol></details>`;
}

export function insightHtml(data, tz) {
  const present = data.public.insight?.state === "present" && data.public.insight.text;
  const line = present
    ? `<p class="assessment-summary">${esc(data.public.insight.text)}</p>`
    : `<p class="sub">${t("No public insight from your AI coach yet.")}</p>`;
  const busy = rewritePending(data.public.rewrite);
  const correction = present
    ? `<div class="wt-field wt-correction">
      <label for="wt-correction">${t("Correct the insight")} <span class="chip">${t("Only you")}</span></label>
      <p class="sub" id="wt-correction-help">${t("Say what is not right, for example “I wasn’t with the group”. Only you see this; your AI coach writes the insight again with it in mind.")}</p>
      <textarea class="btn" id="wt-correction" name="correction" rows="2" maxlength="${esc(data.limits.correction || 2000)}" aria-describedby="wt-correction-help"></textarea>
      <div class="ql-actions"><button class="btn" type="button" data-wt-correct>${t("Rewrite the insight")}</button></div>
      <p class="sub" role="status" data-wt-correct-status></p>
      ${correctionsHtml(data.private.corrections, tz)}
    </div>`
    : "";
  return `<div data-wt-insight aria-busy="${busy ? "true" : "false"}">
    <h4>${t("Insight from your AI coach")}</h4>${line}
    <p class="sub" role="status" data-wt-rewrite>${esc(rewriteText(data.public.rewrite))}</p>
    ${insightVersionsHtml(data.public.insight_versions, tz)}
    ${correction}
  </div>`;
}

export function workoutHtml(data, tz) {
  const { texts, history, limits } = data;
  const stats = data.public.stats.length
    ? `<pre class="publication-description" tabindex="0" aria-label="${esc(t("Statistics"))}">${esc(data.public.stats.join("\n"))}</pre>`
    : `<p class="sub">${t("No figures to publish yet.")}</p>`;
  return `<h2 id="wt-title">${t("Name and texts")}</h2>
    ${textField("name", texts.name, limits.name, history, tz)}
    <section class="wt-part wt-public" aria-labelledby="wt-public-title">
      <h3 id="wt-public-title"><span class="chip info">${t("Public")}</span> ${t("Public part")}</h3>
      <p class="sub">${t("Meant for others. Publishing puts the AI-coach insight and the statistics in the description.")}</p>
      ${insightHtml(data, tz)}
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
          data = { ...data, texts: reply.data.texts, history: reply.data.history, public: reply.data.public };
          redrawInsight(); // the insight may now be written again
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
        data = { ...data, texts: reply.data.texts, history: reply.data.history, public: reply.data.public };
        redrawInsight();
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
        if (on) {
          // Publishing may start the first insight: show that it is being written.
          get("activity-texts", { id }).then((fresh) => {
            if (stale() || !el.isConnected) return;
            data = { ...data, public: fresh.data.public };
            redrawInsight();
          }).catch(() => {});
        }
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

  // The insight part: redrawn on its own, so the texts keep what you are typing.
  let timer = null;
  const POLL_MS = 20000;
  const redrawInsight = (message, focus) => {
    const box = el.querySelector("[data-wt-insight]");
    if (!box) return;
    const typed = box.querySelector("[name=correction]")?.value || "";
    box.outerHTML = insightHtml(data, tz);
    const fresh = el.querySelector("[data-wt-insight]");
    const input = fresh.querySelector("[name=correction]");
    if (input && typed && !message) input.value = typed;
    bindInsight(fresh);
    if (message) fresh.querySelector("[data-wt-correct-status]").textContent = message;
    if (focus) fresh.querySelector(focus)?.focus();
    poll();
  };
  const poll = () => {
    clearTimeout(timer);
    if (!rewritePending(data.public.rewrite)) return;
    timer = setTimeout(async () => {
      if (stale() || !el.isConnected) return;
      const box = el.querySelector("[data-wt-insight]");
      if (box?.contains(document.activeElement) && box.querySelector("[name=correction]")?.value) {
        poll(); // you are typing a correction: look again later
        return;
      }
      try {
        const fresh = (await get("activity-texts", { id })).data;
        if (stale() || !el.isConnected) return;
        data = { ...data, public: fresh.public, private: { ...data.private, corrections: fresh.private.corrections } };
        redrawInsight();
      } catch {
        poll();
      }
    }, POLL_MS);
  };
  const bindInsight = (box) => {
    const button = box.querySelector("[data-wt-correct]");
    if (!button) return;
    const input = box.querySelector("[name=correction]");
    const status = box.querySelector("[data-wt-correct-status]");
    button.addEventListener("click", async () => {
      if (button.getAttribute("aria-disabled") === "true") return;
      const text = input.value.trim();
      status.classList.remove("error-text");
      if (!text) {
        status.textContent = t("Write what is not right first.");
        input.focus();
        return;
      }
      button.setAttribute("aria-disabled", "true");
      status.textContent = t("Saving…");
      try {
        const reply = await post("activity-insight-correction", { id, text });
        if (stale() || !el.isConnected) return;
        data = { ...data, public: reply.data.public, private: { ...data.private, corrections: reply.data.private.corrections } };
        const message = !reply.data.changed.length
          ? t("Nothing changed.")
          : reply.data.queued || rewritePending(reply.data.public.rewrite)
            ? t("Saved. Your AI coach is rewriting the insight.")
            : t("Saved.");
        redrawInsight(message, "[data-wt-correct]");
      } catch (error) {
        button.removeAttribute("aria-disabled");
        status.classList.add("error-text");
        const reason = error.code === "no_insight" ? t("There is no insight from your AI coach to correct yet.") : error.message;
        status.textContent = t("Not saved: {error}", { error: reason });
      }
    });
  };

  el.innerHTML = workoutHtml(data, tz);
  el.hidden = false;
  if (data.texts.name.stored && data.texts.name.text) onName?.(data.texts.name.text);
  el.querySelectorAll("[data-wt-field]").forEach(bindField);
  bindInsight(el.querySelector("[data-wt-insight]"));
  bindPublish();
  poll();
}
