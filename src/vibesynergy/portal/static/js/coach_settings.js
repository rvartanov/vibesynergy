// Coach → Memory: how the coach talks to you, your notes and what to remember.
// Coach → Settings: the name rules for sessions you give as a trainer.
// Everything saves as you edit; the change log keeps every version.
// The plan, its rules and race priorities live in the knowledge base, intervals.icu and
// the start page calendar, so they are not repeated here.
import { get, post } from "./api.js";
import { esc } from "./format.js";
import { N, t } from "./i18n.js";
import { bindAutosave } from "./settings_autosave.js";

const CHOICES = {
  language: [["auto", N("Follow the question")], ["en", "English"], ["nl", "Nederlands"]],
  detail: [["concise", N("Concise")], ["standard", N("Standard")], ["detailed", N("Detailed")]],
  tone: [["direct", N("Direct")], ["supportive", N("Supportive")]],
};
const STYLE = ["language", "detail", "tone"];
const LABELS = { language: N("Advice language"), detail: N("Explanation detail"), tone: N("Feedback style") };
const CONSTRAINT_KINDS = [
  ["availability", N("Availability")],
  ["preference", N("Preference")],
  ["commitment", N("Commitment")],
  ["equipment", N("Equipment")],
  ["self_report", N("Self-report")],
];
// Notes no longer ask for days: the day goes in the text (#139). An older note may still carry
// `days`; they stay as they are, and when its text names none of them, the short day names are
// added to the text once, so the note still says it.
const DAY_SHORT = { 1: N("Mon"), 2: N("Tue"), 3: N("Wed"), 4: N("Thu"), 5: N("Fri"), 6: N("Sat"), 7: N("Sun") };
const DAY_WORDS = {
  1: ["maandag", "ma", "monday", "mon"],
  2: ["dinsdag", "di", "tuesday", "tue"],
  3: ["woensdag", "wo", "wednesday", "wed"],
  4: ["donderdag", "do", "thursday", "thu"],
  5: ["vrijdag", "vr", "friday", "fri"],
  6: ["zaterdag", "za", "saturday", "sat"],
  7: ["zondag", "zo", "sunday", "sun"],
};

export function namesDay(text, day) {
  const words = String(text).toLowerCase().match(/\p{L}+/gu) || [];
  // "vrijdagen", "zondags": a day name at the start of a word counts too.
  return words.some((word) => DAY_WORDS[day]?.some((name) => (name.length > 3 ? word.startsWith(name) : word === name)));
}

export function withDays(text, days) {
  const missing = (days || []).filter((day) => DAY_SHORT[day] && !namesDay(text, day));
  if (!missing.length) return text;
  return `${text} (${missing.map((day) => t(DAY_SHORT[day])).join(", ")})`;
}

const cleanups = new WeakMap();

function select(name, choices, value, id) {
  return `<select name="${name}" id="${id}" class="btn">${choices.map(([key, label]) => `<option value="${esc(key)}"${key === value ? " selected" : ""}>${esc(t(label))}</option>`).join("")}</select>`;
}

async function persistKind(kind, payload, version) {
  const draft = (await post(`coach/${kind}/draft`, { payload, expected_version: version })).data;
  await post(`coach/${kind}/activate`, { expected_version: draft.version });
  return draft.version;
}

export async function coachMemory(el, stale) {
  const { data } = await get("coach");
  if (stale() || !el.isConnected) return;
  draw(el, stale, data);
}

export async function coachSettings(el, stale) {
  if (stale() || !el.isConnected) return;
  cleanups.get(el)?.();
  el.innerHTML = `<section class="card" aria-labelledby="coach-given">
      <h2 id="coach-given">${t("Trainings you give")}</h2>
      <p class="sub">${t("A session you give as a trainer doesn't count as your own training. A title with \"training geven\" is recognised by itself; otherwise choose the purpose \"Coached by me\" on the activity.")}</p>
    </section>
    <p class="sub">${t("How your AI coach talks to you and what it should remember are under Memory.")} <a href="#/coach/memory">${t("Open Memory")}</a></p>`;
}

function draw(el, stale, data) {
  cleanups.get(el)?.();
  const p = { ...data.catalogue.policy_defaults, ...(data.policy.active?.payload || {}) };
  const constraints = data.context.active?.payload?.constraints || [];
  el.innerHTML = `
    <section class="card" data-coach-kind="policy" aria-labelledby="coach-style">
      <h2 id="coach-style">${t("How your AI coach talks to you")}</h2>
      <p class="sub">${t("Your AI coach reads this before giving advice. Your training plan and its rules stay with your AI coach and in VibeSynergy's plan.")}</p>
      <div class="settings-grid">${STYLE.map((key) => `<div class="settings-field"><label for="coach-${key}">${esc(t(LABELS[key]))}</label>${select(key, CHOICES[key], p[key], `coach-${key}`)}</div>`).join("")}</div>
      <div class="settings-field settings-wide"><label for="coach-instructions">${t("Notes for your AI coach")}</label>
        <textarea class="btn" id="coach-instructions" name="instructions" maxlength="2000" rows="4" aria-describedby="coach-notes-hint">${esc(p.instructions || "")}</textarea>
        <p class="sub" id="coach-notes-hint">${t("Your AI coach can read this. Only you can change it.")}</p></div>
      <div data-conflicts></div>
      <p class="sub" data-status role="status" aria-live="polite"></p>
    </section>
    <section class="card" data-coach-kind="context" data-context-version="${esc(String(data.context.version))}" aria-labelledby="coach-remember">
      <h2 id="coach-remember">${t("What to remember")}</h2>
      <p class="sub">${t("Add when you can train, a fixed moment, a preference or your equipment. Your diary and races stay where they already are.")}</p>
      <ul class="memory-list" data-constraints>${constraints.map((entry) => constraintHtml(entry)).join("")}</ul>
      <button class="btn" type="button" data-add-constraint>${t("Add a note")}</button>
      <p class="sub" data-status role="status" aria-live="polite"></p>
    </section>
    <p><a href="#/coach/log?kind=memory">${t("Earlier versions are in the change log")}</a></p>`;

  const policy = el.querySelector('[data-coach-kind="policy"]');
  let policyVersion = data.policy.version;
  const showConflicts = (items) => {
    policy.querySelector("[data-conflicts]").innerHTML = items?.length
      ? `<div class="notice info" role="status">${items.map((item) => `<p>${esc(t(item.explanation))}</p>`).join("")}</div>`
      : "";
  };
  showConflicts(data.policy.active?.conflicts || []);
  const policyPayload = () => ({
    ...Object.fromEntries(STYLE.map((key) => [key, policy.querySelector(`[name="${key}"]`).value])),
    instructions: policy.querySelector('[name="instructions"]').value,
  });
  const policySave = bindAutosave(policy, {
    statusEl: policy.querySelector("[data-status]"),
    save: async () => {
      const draft = (await post("coach/policy/draft", { payload: policyPayload(), expected_version: policyVersion })).data;
      const active = (await post("coach/policy/activate", { expected_version: draft.version })).data;
      policyVersion = draft.version;
      if (!stale() && policy.isConnected) showConflicts(active.active?.conflicts || []);
    },
  });

  const context = el.querySelector('[data-coach-kind="context"]');
  let contextVersion = Number(context.dataset.contextVersion);
  const contextSave = bindAutosave(context, {
    statusEl: context.querySelector("[data-status]"),
    save: async () => {
      contextVersion = await persistKind("context", { constraints: constraintsPayload(context) }, contextVersion);
    },
  });
  const list = context.querySelector("[data-constraints]");
  const open = (row) => {
    for (const other of list.querySelectorAll("[data-constraint].editing")) if (other !== row) close(other, false);
    row.classList.add("editing");
    row.querySelector("[data-edit-constraint]").setAttribute("aria-expanded", "true");
    row.querySelector("[data-constraint-editor]").hidden = false;
    row.querySelector('[name="constraint_text"]').focus();
  };
  const close = (row, focus = true) => {
    const text = row.querySelector('[name="constraint_text"]').value.trim();
    // A new note you leave empty simply goes away.
    if (!text && !row.dataset.archiveKey && !row.dataset.savedText) {
      row.remove();
      if (focus) context.querySelector("[data-add-constraint]").focus();
      return;
    }
    row.classList.remove("editing");
    row.querySelector("[data-constraint-editor]").hidden = true;
    const toggle = row.querySelector("[data-edit-constraint]");
    toggle.setAttribute("aria-expanded", "false");
    if (focus) toggle.focus();
  };
  context.addEventListener("input", (event) => {
    const row = event.target.closest("[data-constraint]");
    if (!row) return;
    if (event.target.name === "constraint_text") row.querySelector("[data-row-text]").textContent = event.target.value.trim() || t("New note");
    if (event.target.name === "constraint_kind") row.querySelector("[data-row-kind]").textContent = kindLabel(event.target.value);
  });
  context.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const row = event.target.closest("[data-constraint].editing");
    if (row) {
      event.preventDefault();
      close(row);
    }
  });
  context.addEventListener("click", (event) => {
    if (event.target.closest("[data-add-constraint]")) {
      list.insertAdjacentHTML("beforeend", constraintHtml({ id: crypto.randomUUID(), kind: "preference", text: "", active: true }));
      open(list.lastElementChild);
      return;
    }
    const toggle = event.target.closest("[data-edit-constraint]");
    if (toggle) {
      const row = toggle.closest("[data-constraint]");
      if (row.classList.contains("editing")) close(row);
      else open(row);
      return;
    }
    const done = event.target.closest("[data-close-constraint]");
    if (done) {
      close(done.closest("[data-constraint]"));
      return;
    }
    const remove = event.target.closest("[data-remove-constraint]");
    if (remove) {
      const row = remove.closest("[data-constraint]");
      // An imported note stays in the history as retired, so a later import cannot add it again.
      if (row.dataset.archiveKey) {
        row.dataset.active = "false";
        row.hidden = true;
      } else {
        row.remove();
      }
      context.querySelector("[data-add-constraint]").focus();
      contextSave.flush();
    }
  });

  cleanups.set(el, () => {
    policySave.unbind();
    contextSave.unbind();
  });
}

function constraintsPayload(root) {
  return [...root.querySelectorAll("[data-constraint]")].flatMap((row) => {
    const typed = row.querySelector('[name="constraint_text"]').value.trim();
    const days = parseList(row.dataset.days);
    let text = typed ? withDays(typed, days) : "";
    let active = row.dataset.active !== "false";
    if (!text && row.dataset.archiveKey) {
      active = false;
      text = row.dataset.savedText || "";
    }
    if (!text) return [];
    const links = parseList(row.dataset.links);
    // Only your own edit makes a coach item yours, not the day names added above.
    const edited = row.dataset.source === "coach" && Boolean(typed) && typed !== row.dataset.savedText;
    return [{
      id: row.dataset.constraint,
      kind: row.querySelector('[name="constraint_kind"]').value,
      text,
      // A coach item whose text you change becomes yours, so the coach no longer edits it.
      source: edited ? "user" : row.dataset.source || "user",
      origin: edited ? "user_statement" : row.dataset.origin || "user_statement",
      active,
      // Kept as they were, for an older note; a new note has none.
      days,
      links,
      archive_key: row.dataset.archiveKey || null,
      imported_digest: row.dataset.importedDigest || null,
    }];
  });
}

function parseList(value) {
  try {
    const parsed = JSON.parse(value || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function kindLabel(kind) {
  const found = CONSTRAINT_KINDS.find(([key]) => key === kind);
  return t(found ? found[1] : "Preference");
}

function provenanceText(entry) {
  if (entry.source === "coach") {
    return entry.origin === "coach_inference" ? t("Your AI coach wrote this: its own conclusion.") : t("Your AI coach wrote this from what you said.");
  }
  if (entry.origin === "coach_inference") return t("A conclusion of your AI coach, not a measurement.");
  if (entry.origin === "measurement") return t("This follows a measurement you already have.");
  if (entry.source === "verified_migration") return t("From notes you confirmed earlier.");
  return t("You wrote this.");
}

// One line per note: its kind and its text. The row opens the note to edit it in place.
function constraintHtml(entry) {
  const hidden = entry.active === false ? " hidden" : "";
  const id = esc(entry.id);
  const kind = entry.kind || "preference";
  return `<li data-constraint="${id}" data-active="${entry.active === false ? "false" : "true"}" data-source="${esc(entry.source || "user")}" data-origin="${esc(entry.origin || "user_statement")}" data-archive-key="${esc(entry.archive_key || "")}" data-imported-digest="${esc(entry.imported_digest || "")}" data-saved-text="${esc(entry.text || "")}" data-days="${esc(JSON.stringify(entry.days || []))}" data-links="${esc(JSON.stringify(entry.links || []))}"${hidden}>
    <button type="button" class="memory-row" data-edit-constraint aria-expanded="false" aria-controls="edit-${id}">
      <span class="memory-kind" data-row-kind>${esc(kindLabel(kind))}</span>
      <span class="memory-text" data-row-text>${esc(entry.text || t("New note"))}</span>
      <span class="memory-edit">${t("Edit")}</span>
    </button>
    <div class="memory-editor" id="edit-${id}" data-constraint-editor hidden>
      <div class="settings-field"><label for="kind-${id}">${t("Context type")}</label>${select("constraint_kind", CONSTRAINT_KINDS, kind, `kind-${id}`)}</div>
      <div class="settings-field"><label for="text-${id}">${t("Note")}</label><textarea class="btn" id="text-${id}" name="constraint_text" rows="3" maxlength="2000">${esc(entry.text || "")}</textarea></div>
      <details><summary>${t("Details")}</summary><p>${esc(provenanceText(entry))}</p></details>
      <div class="memory-actions">
        <button class="btn" type="button" data-close-constraint>${t("Close")}</button>
        <button class="btn" type="button" data-remove-constraint>${t("Remove note")}</button>
      </div>
    </div>
  </li>`;
}

// Name rules: an activity whose name matches is a session you give as a trainer.
