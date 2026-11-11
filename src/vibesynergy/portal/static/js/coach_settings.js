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
const WEEKDAYS = [
  [1, N("Monday")], [2, N("Tuesday")], [3, N("Wednesday")], [4, N("Thursday")],
  [5, N("Friday")], [6, N("Saturday")], [7, N("Sunday")],
];

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
    <p class="sub">${t("How your coach talks to you and what it should remember are under Memory.")} <a href="#/coach/memory">${t("Open Memory")}</a></p>`;
}

function draw(el, stale, data) {
  cleanups.get(el)?.();
  const p = { ...data.catalogue.policy_defaults, ...(data.policy.active?.payload || {}) };
  const constraints = data.context.active?.payload?.constraints || [];
  el.innerHTML = `
    <section class="card" data-coach-kind="policy" aria-labelledby="coach-style">
      <h2 id="coach-style">${t("How your coach talks to you")}</h2>
      <p class="sub">${t("Your coach reads this before giving advice. Your training plan and its rules stay with your coach and in VibeSynergy's plan.")}</p>
      <div class="settings-grid">${STYLE.map((key) => `<div class="settings-field"><label for="coach-${key}">${esc(t(LABELS[key]))}</label>${select(key, CHOICES[key], p[key], `coach-${key}`)}</div>`).join("")}</div>
      <div class="settings-field settings-wide"><label for="coach-instructions">${t("Notes for your coach")}</label>
        <textarea class="btn" id="coach-instructions" name="instructions" maxlength="2000" rows="4" aria-describedby="coach-notes-hint">${esc(p.instructions || "")}</textarea>
        <p class="sub" id="coach-notes-hint">${t("Your coach can read this. Only you can change it.")}</p></div>
      <div data-conflicts></div>
      <p class="sub" data-status role="status" aria-live="polite"></p>
    </section>
    <section class="card" data-coach-kind="context" data-context-version="${esc(String(data.context.version))}" aria-labelledby="coach-remember">
      <h2 id="coach-remember">${t("What to remember")}</h2>
      <p class="sub">${t("Add when you can train, a fixed moment, a preference or your equipment. Your diary and races stay where they already are.")}</p>
      <div data-constraints>${constraints.map((entry) => constraintHtml(entry)).join("")}</div>
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
  context.addEventListener("click", (event) => {
    if (event.target.closest("[data-add-constraint]")) {
      const list = context.querySelector("[data-constraints]");
      list.insertAdjacentHTML("beforeend", constraintHtml({ id: crypto.randomUUID(), kind: "preference", text: "", active: true, days: [] }));
      list.lastElementChild.querySelector('[name="constraint_text"]').focus();
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
    let text = row.querySelector('[name="constraint_text"]').value.trim();
    let active = row.dataset.active !== "false";
    if (!text && row.dataset.archiveKey) {
      active = false;
      text = row.dataset.savedText || "";
    }
    if (!text) return [];
    let links = [];
    try {
      links = JSON.parse(row.dataset.links || "[]");
    } catch {
      links = [];
    }
    return [{
      id: row.dataset.constraint,
      kind: row.querySelector('[name="constraint_kind"]').value,
      text,
      // A coach item whose text you change becomes yours, so the coach no longer edits it.
      source: row.dataset.source === "coach" && text !== row.dataset.savedText ? "user" : row.dataset.source || "user",
      origin: row.dataset.source === "coach" && text !== row.dataset.savedText ? "user_statement" : row.dataset.origin || "user_statement",
      active,
      days: [...row.querySelectorAll('[name="day"]:checked')].map((box) => Number(box.value)),
      links,
      archive_key: row.dataset.archiveKey || null,
      imported_digest: row.dataset.importedDigest || null,
    }];
  });
}

function provenanceText(entry) {
  if (entry.source === "coach") {
    return entry.origin === "coach_inference" ? t("Your coach wrote this: its own conclusion.") : t("Your coach wrote this from what you said.");
  }
  if (entry.origin === "coach_inference") return t("A coach conclusion, not a measurement.");
  if (entry.origin === "measurement") return t("This follows a measurement you already have.");
  if (entry.source === "verified_migration") return t("From notes you confirmed earlier.");
  return t("You wrote this.");
}

function constraintHtml(entry) {
  const days = new Set(entry.days || []);
  const hidden = entry.active === false ? " hidden" : "";
  const id = esc(entry.id);
  return `<div class="card" data-constraint="${id}" data-active="${entry.active === false ? "false" : "true"}" data-source="${esc(entry.source || "user")}" data-origin="${esc(entry.origin || "user_statement")}" data-archive-key="${esc(entry.archive_key || "")}" data-imported-digest="${esc(entry.imported_digest || "")}" data-saved-text="${esc(entry.text || "")}" data-links="${esc(JSON.stringify(entry.links || []))}"${hidden}>
    <div class="settings-grid">
      <div class="settings-field"><label for="kind-${id}">${t("Context type")}</label>${select("constraint_kind", CONSTRAINT_KINDS, entry.kind || "preference", `kind-${id}`)}</div>
      <div class="settings-field"><label for="text-${id}">${t("Note")}</label><textarea class="btn" id="text-${id}" name="constraint_text" rows="2" maxlength="2000">${esc(entry.text || "")}</textarea></div>
    </div>
    <fieldset><legend>${t("Days")}</legend>
      <div class="settings-grid">${WEEKDAYS.map(([day, label]) => `<label class="check"><input type="checkbox" name="day" value="${day}"${days.has(day) ? " checked" : ""}> ${esc(t(label))}</label>`).join("")}</div>
    </fieldset>
    <details><summary>${t("Details")}</summary><p>${esc(provenanceText(entry))}</p></details>
    <button class="btn" type="button" data-remove-constraint>${t("Remove note")}</button>
  </div>`;
}

// Name rules: an activity whose name matches is a session you give as a trainer.
