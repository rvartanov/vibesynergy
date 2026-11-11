// Coach → Knowledge and Coach → Methods & rules. Knowledge is the old Knowledge page:
// articles in use and changes waiting for your decision. Methods & rules shows how the coach
// works: the routine instructions, the working method and the evidence, with their rules.
// Both open the same article page, where text and rules are edited in place and save at once.
// Archived articles leave these views; they stay in the change log.
import { gradeChip } from "./symbols.js";
import { get, post } from "./api.js";
import { coachShell } from "./coach_tabs.js";
import { esc, userDateTime } from "./format.js";
import { N, t } from "./i18n.js";
import { bindAutosave } from "./settings_autosave.js";

const GRADE = {
  A: N("Strong evidence"),
  B: N("Good evidence"),
  C: N("Some evidence"),
  D: N("Weak evidence"),
};
const STATUS = {
  draft: N("Draft"),
  active: N("In use"),
  retired: N("Archived"),
};
const ERRORS = {
  not_found: N("That change is no longer here."),
  not_pending: N("This change was already decided."),
  stale: N("The text changed since this was suggested. Reject it."),
};
const GROUPS = [
  ["prompts", N("Routine instructions"), N("What each coach routine does: morning, evening, week review and the note after a workout.")],
  ["method", N("Working method"), N("How your coach works with your plan and intervals.icu.")],
  ["evidence", N("Scientific basis"), N("What the research says, and the rules your coach takes from it.")],
];
const STALE_EDIT = N("Your coach changed this in the meantime. Reload to see the latest text before you edit.");
const cleanups = new WeakMap();

export function gradeLabel(code) {
  return t(GRADE[code] || code || "");
}

export function statusLabel(code) {
  return t(STATUS[code] || code || "");
}

function proposalName(proposal) {
  const current = proposal.current;
  if (!current) {
    const name = proposal.changes.title || proposal.changes.citation || proposal.target;
    if (proposal.kind === "rule") return t("New rule: {name}", { name: proposal.target });
    if (proposal.kind === "source") return t("New source: {name}", { name });
    return t("New article: {name}", { name });
  }
  if (proposal.kind === "rule") return t("Change to {name}", { name: proposal.target });
  return t("Change to {name}", { name: current.title || current.article_title || proposal.target });
}

function textBlock(label, value) {
  if (!value) return "";
  return `<h3>${esc(label)}</h3><div class="kb-body">${esc(value)}</div>`;
}

function proposalBody(proposal) {
  const changes = proposal.changes || {};
  const current = proposal.current || {};
  const next = changes.text || changes.body_md || changes.citation || "";
  const previous = current.text || current.body_md || current.citation || "";
  const lines = [];
  if (next) lines.push(textBlock(t("It would say"), next));
  if (previous && previous !== next) lines.push(textBlock(t("It says now"), previous));
  if (changes.title && changes.title !== current.title) {
    lines.push(`<p>${esc(t("Title would be {title}.", { title: changes.title }))}</p>`);
    if (current.title) lines.push(`<p>${esc(t("Title is {title} now.", { title: current.title }))}</p>`);
  }
  const nextGrade = changes.evidence_grade || changes.grade;
  const prevGrade = current.evidence_grade || current.grade;
  if (nextGrade && nextGrade !== prevGrade) {
    lines.push(`<p>${esc(t("Evidence would be {grade}.", { grade: gradeLabel(nextGrade) }))}</p>`);
    if (prevGrade) lines.push(`<p>${esc(t("Evidence is {grade} now.", { grade: gradeLabel(prevGrade) }))}</p>`);
  }
  if (changes.topic && changes.topic !== current.topic) {
    lines.push(`<p>${esc(t("Topic would be {topic}.", { topic: changes.topic }))}</p>`);
  }
  if (changes.status && changes.status !== current.status) {
    lines.push(`<p>${esc(t("Status would be {status}.", { status: statusLabel(changes.status) }))}</p>`);
  }
  if (typeof changes.active === "boolean" && changes.active !== current.active) {
    lines.push(`<p>${esc(changes.active ? t("Turn this on.") : t("Turn this off."))}</p>`);
    if (typeof current.active === "boolean") {
      lines.push(`<p>${esc(current.active ? t("It is on now.") : t("It is off now."))}</p>`);
    }
  }
  if (changes.url) lines.push(`<p><a href="${esc(changes.url)}">${esc(changes.url)}</a></p>`);
  return lines.join("");
}

function proposalCard(proposal, { canDecide }) {
  return `<article class="kb-proposal">
    <h3>${esc(proposalName(proposal))}</h3>
    ${proposalBody(proposal)}
    <p><strong>${esc(t("Why"))}</strong> ${esc(proposal.rationale)}</p>
    <details>
      <summary>${esc(t("Details"))}</summary>
      <p class="sub">${esc(t("Version {version}", { version: proposal.base_version }))}</p>
    </details>
    ${canDecide ? `<div class="kb-actions">
      <button class="btn primary" type="button" data-decide="approve" data-id="${esc(proposal.id)}">${esc(t("Approve"))}</button>
      <button class="btn" type="button" data-decide="reject" data-id="${esc(proposal.id)}">${esc(t("Reject"))}</button>
    </div>` : ""}
  </article>`;
}

function articleHref(base, slug) {
  return `#/coach/${base}/${encodeURIComponent(slug)}`;
}

function articleLink(article) {
  return `<li><a href="${articleHref("knowledge", article.slug)}">${esc(article.title)}</a>
    <div class="sub">${esc(article.topic)} ${gradeChip(article.evidence_grade, gradeLabel(article.evidence_grade))}</div></li>`;
}

function sourceLine(source) {
  const link = source.url ? ` <a href="${esc(source.url)}">${esc(source.url)}</a>` : "";
  return `<li>${esc(source.citation)}${link}</li>`;
}

// One rule, edited in place: its text and whether the coach uses it. Saves as you type.
function ruleEditor(rule, { level = 3, readOnly = false } = {}) {
  const id = esc(rule.id);
  const heading = `<h${level} class="kb-rule-id">${id}</h${level}>`;
  if (readOnly) {
    return `<article class="kb-rule">${heading}<div class="kb-body">${esc(rule.text)}</div>
      <p class="sub">${gradeChip(rule.grade, gradeLabel(rule.grade))}${rule.active ? "" : ` · ${esc(t("Off"))}`}</p></article>`;
  }
  return `<article class="kb-rule" data-rule="${id}" data-version="${esc(String(rule.version))}">
    ${heading}
    <div class="settings-field settings-wide"><label for="rule-text-${id}">${esc(t("Rule text"))}</label>
      <textarea class="btn" id="rule-text-${id}" name="rule_text" rows="3" maxlength="4000">${esc(rule.text)}</textarea></div>
    <label class="check"><input type="checkbox" name="rule_active"${rule.active ? " checked" : ""}> ${esc(t("Your coach uses this rule"))}</label>
    <p class="sub">${gradeChip(rule.grade, gradeLabel(rule.grade))}</p>
    <p class="sub" data-status role="status" aria-live="polite"></p>
  </article>`;
}

function staleHandler(statusEl, reload) {
  return (error) => {
    if (error?.message !== "stale") return false;
    statusEl.classList.add("error-text");
    statusEl.replaceChildren();
    const text = document.createElement("span");
    text.textContent = t(STALE_EDIT);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn";
    button.textContent = t("Reload");
    button.addEventListener("click", () => reload());
    statusEl.append(text, document.createTextNode(" "), button);
    return true;
  };
}

function bindRules(root, reload) {
  const unbinds = [];
  for (const card of root.querySelectorAll("[data-rule]")) {
    const statusEl = card.querySelector("[data-status]");
    const saver = bindAutosave(card, {
      statusEl,
      onError: staleHandler(statusEl, reload),
      save: async () => {
        const { data } = await post("knowledge/rule/edit", {
          id: card.dataset.rule,
          expected_version: Number(card.dataset.version),
          text: card.querySelector('[name="rule_text"]').value,
          active: card.querySelector('[name="rule_active"]').checked,
        });
        card.dataset.version = String(data.rule.version);
      },
    });
    unbinds.push(() => saver.unbind());
  }
  return () => unbinds.forEach((unbind) => unbind());
}

// Coach → Knowledge ----------------------------------------------------------------------

export async function renderKnowledge(view, { args = [], stale, share = null }) {
  if (args[0]) return renderArticle(view, "knowledge", args[0], { stale, share });
  const reply = await get("knowledge");
  if (stale()) return;
  const data = reply.data;
  const proposals = data.proposals || [];
  const articles = data.articles || [];
  const body = coachShell(view, "knowledge", { intro: t("Articles and changes waiting for you."), share });
  const archived = !share && data.archived
    ? `<p class="sub">${esc(t("Archived articles: {count}.", { count: data.archived }))} <a href="#/coach/log?view=archive">${esc(t("Open the archive"))}</a></p>`
    : "";
  body.innerHTML = `
    <p class="notice info" id="kb-status" role="status" hidden></p>
    <section class="card" aria-labelledby="kb-waiting">
      <h2 id="kb-waiting">${esc(t("Changes waiting for you"))}</h2>
      ${proposals.length
        ? proposals.map((proposal) => proposalCard(proposal, { canDecide: !share || share.can_write })).join("")
        : `<p class="empty">${esc(t("Nothing is waiting."))}</p>`}
    </section>
    <section class="card" aria-labelledby="kb-articles">
      <h2 id="kb-articles">${esc(t("Articles"))}</h2>
      ${articles.length
        ? `<ul class="kb-list">${articles.map(articleLink).join("")}</ul>`
        : `<p class="empty">${esc(t("No articles yet."))}</p>`}
      ${archived}
    </section>`;
  bindDecisions(view, stale, () => renderKnowledge(view, { args, stale, share }));
}

// Coach → Methods & rules ------------------------------------------------------------------

export async function renderMethods(view, { args = [], stale, share = null }) {
  if (args[0]) return renderArticle(view, "methods", args[0], { stale, share });
  const { data } = await get("knowledge/methods");
  if (stale()) return;
  const body = coachShell(view, "methods", {
    intro: t("How your coach works, and the rules it follows. Change a text here and your coach uses it from the next run."),
  });
  body.innerHTML = GROUPS.map(([key, label, hint]) => {
    const articles = data.groups?.[key] || [];
    const empty = key === "prompts"
      ? `<p class="empty">${esc(t("The routine instructions are not here yet."))}</p>
         <button class="btn" type="button" data-seed-prompts>${esc(t("Add the routine instructions"))}</button>
         <p class="sub" data-seed-status role="status" aria-live="polite"></p>`
      : `<p class="empty">${esc(t("Nothing here yet."))}</p>`;
    return `<section class="card" aria-labelledby="methods-${key}">
      <h2 id="methods-${key}">${esc(t(label))}</h2>
      <p class="sub">${esc(t(hint))}</p>
      ${articles.length ? articles.map((article) => methodCard(article)).join("") : empty}
    </section>`;
  }).join("");
  cleanups.get(view)?.();
  cleanups.set(view, bindRules(body, () => renderMethods(view, { args, stale, share })));
  body.querySelector("[data-seed-prompts]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const status = body.querySelector("[data-seed-status]");
    button.disabled = true;
    status.textContent = t("Saving…");
    try {
      await post("knowledge/seed-prompts");
      if (stale() || !view.isConnected) return;
      await renderMethods(view, { args, stale, share });
      view.querySelector("#methods-prompts")?.setAttribute("tabindex", "-1");
      view.querySelector("#methods-prompts")?.focus();
    } catch {
      if (stale() || !view.isConnected) return;
      button.disabled = false;
      status.textContent = t("Couldn't save. Check your connection and try again.");
    }
  });
}

function methodCard(article) {
  const rules = article.rules || [];
  const meta = [
    // A routine instruction is not a research claim, so it shows no evidence grade.
    article.topic === "coach-prompts" ? "" : gradeLabel(article.evidence_grade),
    rules.length ? t("Rules: {count}", { count: rules.length }) : "",
    article.sources ? t("Sources: {count}", { count: article.sources }) : "",
    article.updated_at ? t("Changed {when}", { when: userDateTime(article.updated_at) }) : "",
  ].filter(Boolean);
  return `<article class="kb-method">
    <h3><a href="${articleHref("methods", article.slug)}">${esc(article.title)}</a></h3>
    <p class="sub">${esc(meta.join(" · "))}</p>
    ${rules.length ? `<details data-refresh-key="method-rules-${esc(article.slug)}"><summary>${esc(t("Rules"))} (${rules.length})</summary>
      ${rules.map((rule) => ruleEditor(rule, { level: 4 })).join("")}
    </details>` : ""}
  </article>`;
}

// One article, in place --------------------------------------------------------------------

async function renderArticle(view, tab, slug, { stale, share }) {
  let reply;
  try {
    reply = await get("knowledge/article", { slug });
  } catch {
    if (stale()) return;
    const body = coachShell(view, tab, { share });
    body.innerHTML = `<div class="notice error" role="alert">${esc(t("That article is no longer here."))}</div>
      <p><a href="#/coach/${tab}">${esc(t("Back to the list"))}</a></p>`;
    return;
  }
  if (stale()) return;
  const article = reply.data;
  const readOnly = Boolean(share);
  const archived = article.status !== "active";
  const body = coachShell(view, tab, { heading: article.title, share });
  const rules = article.rules || [];
  const sources = article.sources || [];
  const reload = () => renderArticle(view, tab, slug, { stale, share });
  const text = readOnly
    ? `<div class="kb-body">${esc(article.body_md)}</div>`
    : `<div class="settings-field settings-wide"><label for="kb-title">${esc(t("Title"))}</label>
        <input class="btn" id="kb-title" name="title" maxlength="200" value="${esc(article.title)}"></div>
      <div class="settings-field settings-wide"><label for="kb-text">${esc(t("Text"))}</label>
        <textarea class="btn kb-editor" id="kb-text" name="body_md" rows="16" maxlength="50000">${esc(article.body_md)}</textarea></div>
      <p class="sub" data-status role="status" aria-live="polite"></p>`;
  body.innerHTML = `
    <p><a href="#/coach/${tab}">${esc(t("Back to the list"))}</a></p>
    ${archived ? `<p class="notice info" role="status">${esc(t("This article is archived. Your coach pages no longer show it."))}</p>` : ""}
    <section class="card" data-article aria-labelledby="kb-article-text" data-version="${esc(String(article.version))}">
      <h2 id="kb-article-text">${esc(t("Article"))}</h2>
      <p class="sub">${esc([article.topic, statusLabel(article.status), gradeLabel(article.evidence_grade), t("Changed {when}", { when: userDateTime(article.updated_at) })].join(" · "))}</p>
      ${text}
      ${readOnly ? "" : `<div class="kb-actions">
        <button class="btn" type="button" data-archive>${esc(archived ? t("Bring back") : t("Archive"))}</button>
        <a class="btn" href="#/coach/log?kind=article&amp;target=${encodeURIComponent(slug)}">${esc(t("See all changes"))}</a>
      </div>
      <p class="sub" data-archive-status role="status" aria-live="polite" tabindex="-1"></p>`}
    </section>
    <section class="card" aria-labelledby="kb-rules">
      <h2 id="kb-rules">${esc(t("Rules"))}</h2>
      ${rules.length
        ? rules.map((rule) => ruleEditor(rule, { readOnly })).join("")
        : `<p class="empty">${esc(t("No rules on this article."))}</p>`}
    </section>
    <section class="card" aria-labelledby="kb-sources">
      <h2 id="kb-sources">${esc(t("Sources"))}</h2>
      ${sources.length
        ? `<ul class="kb-list">${sources.map(sourceLine).join("")}</ul>`
        : `<p class="empty">${esc(t("No sources on this article."))}</p>`}
    </section>`;
  if (readOnly) return;
  const card = body.querySelector("[data-article]");
  const statusEl = card.querySelector("[data-status]");
  const saver = bindAutosave(card, {
    statusEl,
    onError: staleHandler(statusEl, reload),
    save: async () => {
      const { data } = await post("knowledge/article/edit", {
        slug,
        expected_version: Number(card.dataset.version),
        title: card.querySelector('[name="title"]').value,
        body_md: card.querySelector('[name="body_md"]').value,
      });
      card.dataset.version = String(data.version);
    },
  });
  const unbindRules = bindRules(body, reload);
  cleanups.get(view)?.();
  cleanups.set(view, () => {
    saver.unbind();
    unbindRules();
  });
  card.querySelector("[data-archive]").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const note = card.querySelector("[data-archive-status]");
    button.disabled = true;
    try {
      await saver.flush();
      const { data } = await post("knowledge/article/edit", {
        slug,
        expected_version: Number(card.dataset.version),
        status: archived ? "active" : "retired",
      });
      if (stale() || !view.isConnected) return;
      card.dataset.version = String(data.version);
      await reload();
      const fresh = view.querySelector("[data-archive-status]");
      if (fresh) {
        fresh.textContent = archived
          ? t("Back in use.")
          : t("Archived. It stays in the change log, and you can bring it back here.");
        fresh.focus();
      }
    } catch (error) {
      if (stale() || !view.isConnected) return;
      button.disabled = false;
      if (!staleHandler(note, reload)(error)) note.textContent = t("Couldn't save. Check your connection and try again.");
    }
  });
}

function bindDecisions(view, stale, refresh) {
  const status = view.querySelector("#kb-status");
  for (const button of view.querySelectorAll("[data-decide]")) {
    button.addEventListener("click", async () => {
      const action = button.dataset.decide;
      const id = button.dataset.id;
      for (const control of view.querySelectorAll("[data-decide]")) control.disabled = true;
      try {
        await post(`knowledge/proposal/${action}`, { id });
        if (stale() || !view.isConnected) return;
        await refresh();
        const note = view.querySelector("#kb-status");
        if (note) {
          note.hidden = false;
          note.className = "notice";
          note.textContent = action === "approve" ? t("Approved") : t("Rejected");
        }
      } catch (error) {
        if (stale() || !view.isConnected) return;
        for (const control of view.querySelectorAll("[data-decide]")) control.disabled = false;
        status.hidden = false;
        status.className = "notice error";
        status.setAttribute("role", "alert");
        status.tabIndex = -1;
        status.textContent = t(ERRORS[error.message] || N("Could not save that decision."));
        status.focus();
      }
    });
  }
}
