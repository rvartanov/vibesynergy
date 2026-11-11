// Quick log on the Today page: how you feel (one tap per question, saved at once), a note
// (for the day or one activity, optionally with pain), a niggle, and pain today for
// complaints that are still open. It writes to the diary; the
// Diary page has the full history and editing.
import { get, post } from "./api.js";
import { esc, number } from "./format.js";
import { N, t } from "./i18n.js";

const QUESTIONS = [
  ["sleep_quality", N("Sleep"), N("poor"), N("good")],
  ["soreness", N("Soreness"), N("none"), N("very sore")],
  ["mood", N("Mood"), N("sad"), N("cheerful")],
  ["stress", N("Stress"), N("calm"), N("stressed")],
  ["motivation", N("Motivation"), N("low"), N("high")],
];
const DASHBOARD_QUESTIONS = [
  ["sleep_quality", N("Sleep"), N("poor"), N("good")],
  ["soreness", N("Legs"), N("fresh"), N("heavy")],
  ["mood", N("Mood"), N("sad"), N("cheerful")],
  ["stress", N("Stress"), N("calm"), N("stressed")],
  ["motivation", N("Motivation"), N("low"), N("high")],
];

function qlScaleHtml(field, checkin, label, low, high) {
  const dot = (n) =>
    `<button type="button" class="ql-dot${checkin[field] === n ? " on" : ""}" data-field="${field}" data-value="${n}" aria-pressed="${checkin[field] === n}">${n}</button>`;
  const end = (n, text) => `<span class="ql-scale-end">${dot(n)}<span class="ql-end-label" aria-hidden="true">${esc(text)}</span></span>`;
  return `<span class="ql-scale" role="group" aria-label="${esc(label)}, 1 = ${esc(low)}, 5 = ${esc(high)}">${end(1, low)}${dot(2)}${dot(3)}${dot(4)}${end(5, high)}</span>`;
}
const PART_NAMES = (part) => t(part.replaceAll("_", " "));

export async function mountQuickLog(el, meta, stale, options = {}) {
  const share = meta.data.share;
  if (share && !(share.pages.includes("diary") && share.can_write)) {
    el.remove(); // a shared view that may not write to the diary
    return;
  }
  const variant = options.variant || "page";
  const reply = await get("diary", { period: "last_7d" });
  if (stale()) return;
  draw(el, reply.data, meta.data.today, stale, "", variant);
}

function draw(el, diary, today, stale, flash = "", variant = "page") {
  const dashboard = variant === "dashboard";
  const questions = dashboard ? DASHBOARD_QUESTIONS : QUESTIONS;
  const checkin = diary.checkins.find((c) => c.date === today) || {};
  const open = diary.injuries.filter((i) => i.status !== "resolved");
  const choices = diary.choices;
  const answered = questions.every(([field]) => checkin[field] != null);
  el.openForm = (name) => {
    el.dataset.editing = "1";
    el.dataset.openForm = name;
    draw(el, diary, today, stale, flash, variant);
  };
  if (dashboard && answered && el.dataset.editing !== "1") {
    el.innerHTML = `${flash ? `<div class="sub ok-text" role="status">${esc(flash)}</div>` : ""}<p class="checkin-done">${esc(t("Checked in"))} <span aria-hidden="true">✓</span> · <button type="button" class="btn" data-edit-checkin>${t("Edit check-in")}</button></p>
      <div class="ql-actions"><button class="btn" type="button" data-compact-open="note">+ ${t("Note")}</button><button class="btn" type="button" data-compact-open="pain">+ ${t("Pain")}</button></div>`;
    el.querySelector("[data-edit-checkin]").addEventListener("click", () => {
      el.dataset.editing = "1";
      draw(el, diary, today, stale, "", variant);
    });
    for (const button of el.querySelectorAll("[data-compact-open]")) {
      button.addEventListener("click", () => el.openForm(button.dataset.compactOpen === "pain" ? "note-pain" : "note"));
    }
    return;
  }
  const openForm = el.dataset.openForm || "";
  delete el.dataset.openForm;
  el.innerHTML = `
    ${dashboard ? "" : `<div class="page-head"><h2>${t("Quick log")}</h2><a class="sub" href="#/dashboard?section=diary">${t("Diary")}</a></div>`}
    ${flash ? `<div class="sub ok-text">${esc(flash)}</div>` : ""}
    <div class="ql-questions">${questions.map(
      ([field, name, lowText, highText]) => {
        const [label, low, high] = [t(name), t(lowText), t(highText)];
        return `<div class="ql-q"><span class="ql-label" title="1 = ${esc(low)} … 5 = ${esc(high)}">${esc(label)}</span>
        ${qlScaleHtml(field, checkin, label, low, high)}</div>`;
      },
    ).join("")}</div>
    ${!dashboard && open.length ? `<div class="ql-complaints">${open
      .map((i) => {
        const trend = i.pain_log.map((e) => e.pain).join(" → ");
        const todayPain = i.pain_log.find((e) => e.date === today)?.pain;
        return `<div class="ql-complaint"><span><b>${esc(i.title)}</b> <span class="muted">${esc(trend || t("no pain logged"))}</span></span>
          <label class="sub">${t("Pain today")} <select class="btn" data-pain="${esc(i.id)}"><option value="">–</option>${Array.from({ length: 11 }, (_, n) => `<option${todayPain === n ? " selected" : ""}>${n}</option>`).join("")}</select></label></div>`;
      })
      .join("")}</div>` : ""}
    <div class="ql-actions">
      <button class="btn" type="button" data-open="note">+ ${t("Note")}</button>
      <button class="btn" type="button" data-open="${dashboard ? "note-pain" : "niggle"}">+ ${dashboard ? t("Pain") : t("Niggle")}</button>
    </div>
    ${noteFormHtml(choices, { activities: diary.events.filter((event) => event.local_date === today), key: variant })}
    <form class="ql-form" data-form="niggle" hidden>
      <div class="settings-grid">
        <label>${t("Where")} <select class="btn" name="body_part" required><option value="">${t("Choose…")}</option>${choices.body_parts.map((p) => `<option value="${esc(p)}">${esc(PART_NAMES(p))}</option>`).join("")}</select></label>
        <label>${t("Side")} <select class="btn" name="side">${choices.sides.map((s) => `<option value="${esc(s)}"${s === "left" ? " selected" : ""}>${esc(t(s))}</option>`).join("")}</select></label>
        <label>${t("Pain (0–10)")} <select class="btn" name="pain">${Array.from({ length: 11 }, (_, n) => `<option${n === 2 ? " selected" : ""}>${n}</option>`).join("")}</select></label>
        <label>${t("When it hurts")} <select class="btn" name="timing"><option value="">–</option>${choices.timings.map((w) => `<option value="${esc(w)}">${esc(t(w))}</option>`).join("")}</select></label>
      </div>
      <label class="check"><input type="checkbox" name="affects_movement"> ${t("It changes how I move")}</label>
      <input class="btn wide" name="note" maxlength="2000" placeholder="${esc(t("Optional: what happened, what makes it worse"))}">
      <div class="settings-save"><button class="btn primary" type="submit">${t("Log niggle")}</button><span class="sub" data-status></span></div>
    </form>
    ${dashboard ? "" : `<p class="sub">${esc(t(diary.note || ""))}</p>`}`;
  if (openForm) {
    const form = el.querySelector(`[data-form="${openForm === "note-pain" ? "note" : openForm}"]`);
    if (form) {
      form.hidden = false;
      if (openForm === "note-pain") setPain(form, true);
      form.querySelector("textarea, input, select")?.focus();
    }
  }

  const refresh = async (message) => {
    const reply = await get("diary", { period: "last_7d" });
    if (!stale()) {
      if (dashboard) delete el.dataset.editing;
      draw(el, reply.data, today, stale, message, variant);
    }
  };
  const fail = (target, error) => {
    const status = target.querySelector?.("[data-status]") || el.querySelector("[data-status]");
    if (status) {
      status.textContent = error.message;
      status.classList.add("error-text");
    }
  };

  el.querySelectorAll(".ql-dot").forEach((button) =>
    button.addEventListener("click", async () => {
      try {
        await post("diary/checkin", { [button.dataset.field]: Number(button.dataset.value) });
        refresh(t("Saved."));
      } catch (error) {
        fail(el, error);
      }
    }),
  );
  el.querySelectorAll("[data-pain]").forEach((select) =>
    select.addEventListener("change", async () => {
      if (select.value === "") return;
      try {
        await post("diary/injury", { injury_id: select.dataset.pain, pain: Number(select.value) });
        refresh(t("Pain logged."));
      } catch (error) {
        fail(el, error);
      }
    }),
  );
  el.querySelectorAll("[data-open]").forEach((button) =>
    button.addEventListener("click", () => {
      // "note-pain": the note form with pain ticked, so the pain lands in the pain log too.
      const pain = button.dataset.open === "note-pain";
      const form = el.querySelector(`[data-form="${pain ? "note" : button.dataset.open}"]`);
      form.hidden = pain ? false : !form.hidden;
      if (pain) setPain(form, true);
      form.querySelector("textarea, input, select")?.focus();
    }),
  );
  bindNoteForm(el.querySelector('[data-form="note"]'), {
    onSaved: (message) => refresh(message),
  });
  el.querySelector('[data-form="niggle"]').addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const field = (name) => form.querySelector(`[name=${name}]`);
    try {
      await post("diary/injury", {
        body_part: field("body_part").value,
        side: field("side").value,
        kind: "niggle",
        pain: Number(field("pain").value),
        timing: field("timing").value || null,
        affects_movement: field("affects_movement").checked,
        note: field("note").value || null,
      });
      refresh(t("Logged: pain {n}/10.", { n: number(Number(field("pain").value)) }));
    } catch (error) {
      fail(form, error);
    }
  });
}

// The diary note: free text for the day, optionally about one activity, with tags. Ticking
// "pain" shows where it hurts and how much; saving then also logs it in the injury log, so
// the pain history and its warnings keep working.
function painFieldsHtml(choices, key) {
  const id = (name) => `note-${key}-${name}`;
  return `<fieldset class="ql-pain" data-pain-fields hidden>
      <legend>${t("Where it hurts")}</legend>
      <div class="settings-grid">
        <label>${t("Where")} <select class="btn" id="${id("part")}" name="body_part"><option value="">${t("Choose…")}</option>${choices.body_parts.map((p) => `<option value="${esc(p)}">${esc(PART_NAMES(p))}</option>`).join("")}</select></label>
        <label>${t("Side")} <select class="btn" id="${id("side")}" name="side">${choices.sides.map((s) => `<option value="${esc(s)}"${s === "left" ? " selected" : ""}>${esc(t(s))}</option>`).join("")}</select></label>
        <label>${t("Pain (0–10)")} <select class="btn" id="${id("pain")}" name="pain">${Array.from({ length: 11 }, (_, n) => `<option${n === 2 ? " selected" : ""}>${n}</option>`).join("")}</select></label>
        <label>${t("When it hurts")} <select class="btn" id="${id("timing")}" name="timing"><option value="">–</option>${choices.timings.map((w) => `<option value="${esc(w)}">${esc(t(w))}</option>`).join("")}</select></label>
      </div>
      <label class="check"><input type="checkbox" name="affects_movement"> ${t("It changes how I move")}</label>
    </fieldset>`;
}

function activityName(event) {
  return event.name || t("Activity");
}

// activities: today's activities to choose from; activityId: a fixed activity (activity page).
export function noteFormHtml(choices, { activities = [], activityId = null, key = "page", hidden = true } = {}) {
  const id = (name) => `note-${key}-${name}`;
  const about = !activityId && activities.length
    ? `<label for="${id("about")}">${t("About")}</label>
      <select class="btn" id="${id("about")}" name="activity_id"><option value="">${t("The whole day")}</option>${activities
        .map((event) => `<option value="${esc(event.sources?.[0] ?? event.group_id)}">${esc(activityName(event))}</option>`)
        .join("")}</select>`
    : "";
  return `<form class="ql-form" data-form="note"${hidden ? " hidden" : ""}${activityId ? ` data-activity-id="${esc(activityId)}"` : ""}>
      <label for="${id("text")}">${t("Note")}</label>
      <textarea class="btn wide" id="${id("text")}" name="text" rows="3" maxlength="2000" required placeholder="${esc(t("e.g. Late dinner, slept badly; legs heavy on the run"))}"></textarea>
      ${about}
      <fieldset class="share-pages"><legend class="sub">${t("Tags")}</legend>${choices.tags
        .map((tag) => `<label class="check"><input type="checkbox" name="tag" value="${esc(tag)}"> ${esc(t(tag.replaceAll("_", " ")))}</label>`)
        .join("")}</fieldset>
      ${painFieldsHtml(choices, key)}
      <div class="settings-save"><button class="btn primary" type="submit">${t("Save note")}</button><span class="sub" data-status role="status" aria-live="polite"></span></div>
    </form>`;
}

function setPain(form, on) {
  const box = form.querySelector('[name=tag][value="pain"]');
  if (box) box.checked = on;
  const fields = form.querySelector("[data-pain-fields]");
  if (fields) fields.hidden = !on;
}

export function bindNoteForm(form, { onSaved } = {}) {
  if (!form) return;
  const painBox = form.querySelector('[name=tag][value="pain"]');
  painBox?.addEventListener("change", () => setPain(form, painBox.checked));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const field = (name) => form.querySelector(`[name=${name}]`);
    const status = form.querySelector("[data-status]");
    status.classList.remove("error-text");
    const text = field("text").value;
    const tags = [...form.querySelectorAll("[name=tag]:checked")].map((c) => c.value);
    const pain = tags.includes("pain");
    if (pain && !field("body_part").value) {
      status.textContent = t("Choose where it hurts.");
      status.classList.add("error-text");
      field("body_part").focus();
      return;
    }
    const activity = form.dataset.activityId || field("activity_id")?.value || null;
    try {
      const saved = await post("diary/note", { text, tags, activity_id: activity });
      if (pain) {
        await post("diary/injury", {
          body_part: field("body_part").value,
          side: field("side").value,
          kind: "pain",
          pain: Number(field("pain").value),
          timing: field("timing").value || null,
          affects_movement: field("affects_movement").checked,
          note: text,
        });
      }
      const message = pain
        ? t("Note saved; pain {n}/10 is in your pain log.", { n: number(Number(field("pain").value)) })
        : t("Note saved.");
      onSaved?.(message, saved.data);
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("error-text");
    }
  });
}
