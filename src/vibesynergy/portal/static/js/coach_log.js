// Coach → Change log: every version of the knowledge, rules, sources and the coach's
// memory, newest first. Open one to see what changed (removed and added lines) and to go
// back. Going back adds a new version; nothing is deleted. Archived articles live here too.
import { get, post } from "./api.js";
import { coachShell } from "./coach_tabs.js";
import { gradeLabel, statusLabel } from "./coach_knowledge.js";
import { esc, userDateTime } from "./format.js";
import { N, t } from "./i18n.js";

const KINDS = {
  article: N("Article"),
  rule: N("Rule"),
  source: N("Source"),
  policy: N("How your coach talks to you"),
  context: N("What to remember"),
};
const FILTERS = [
  ["", N("Everything")],
  ["article", N("Articles")],
  ["rule", N("Rules")],
  ["source", N("Sources")],
  ["memory", N("Memory")],
];
const FIELDS = {
  created: N("Added"),
  title: N("Title"),
  body_md: N("Text"),
  text: N("Text"),
  citation: N("Citation"),
  instructions: N("Notes for your coach"),
  constraints: N("What to remember"),
  topic: N("Topic"),
  evidence_grade: N("Evidence"),
  grade: N("Evidence"),
  status: N("Status"),
  active: N("In use"),
  article: N("Article"),
  doi: N("DOI"),
  url: N("Link"),
  language: N("Advice language"),
  detail: N("Explanation detail"),
  tone: N("Feedback style"),
};
const STYLE_VALUES = {
  auto: N("Follow the question"),
  en: "English",
  nl: "Nederlands",
  concise: N("Concise"),
  standard: N("Standard"),
  detailed: N("Detailed"),
  direct: N("Direct"),
  supportive: N("Supportive"),
};
const AUTHORS = {
  user: N("You"),
  portal: N("You"),
  ai_coach: N("AI coach"),
  legacy: N("Coach (before identities)"),
  system: N("System"),
};

function who(author, names) {
  if (AUTHORS[author]) return t(AUTHORS[author]);
  if (String(author).startsWith("seed:")) return t("First copy");
  return names[author] || (String(author).startsWith("client:") ? t("AI coach") : author);
}

function value(field, raw) {
  if (raw === null || raw === undefined || raw === "") return "—";
  if (field === "status") return statusLabel(raw);
  if (field === "evidence_grade" || field === "grade") return gradeLabel(raw);
  if (field === "active") return raw ? t("On") : t("Off");
  if (STYLE_VALUES[raw] && ["language", "detail", "tone"].includes(field)) return t(STYLE_VALUES[raw]);
  return String(raw);
}

function entryTitle(entry) {
  const kind = t(KINDS[entry.kind] || entry.kind);
  return entry.title ? `${kind}: ${entry.title}` : kind;
}

function entryHtml(entry, names) {
  const fields = (entry.fields || []).map((field) => t(FIELDS[field] || field)).join(", ");
  const key = `${entry.kind}:${entry.target}:${entry.version}`;
  return `<li class="coach-log-entry">
    <details data-log="${esc(key)}" data-kind="${esc(entry.kind)}" data-target="${esc(entry.target)}" data-version="${entry.version}" data-latest="${entry.latest ? "1" : ""}">
      <summary><span class="coach-log-what">${esc(entryTitle(entry))}</span>
        <span class="sub">${esc(t("Version {version}", { version: entry.version }))} · ${esc(who(entry.author, names))} · ${esc(userDateTime(entry.at))}${fields ? ` · ${esc(fields)}` : ""}</span></summary>
      <div data-diff><p class="sub" role="status" aria-busy="true">${esc(t("Loading…"))}</p></div>
    </details>
  </li>`;
}

function linesHtml(lines) {
  return `<div class="coach-diff">${lines
    .map((line) => {
      if (line.op === "gap") return `<p class="coach-diff-gap">${esc(t("… {count} lines unchanged", { count: line.text }))}</p>`;
      if (line.op === "add") return `<ins><span class="visually-hidden">${esc(t("Added:"))} </span><span aria-hidden="true">+ </span>${esc(line.text)}</ins>`;
      if (line.op === "del") return `<del><span class="visually-hidden">${esc(t("Removed:"))} </span><span aria-hidden="true">− </span>${esc(line.text)}</del>`;
      return `<p class="coach-diff-same"><span aria-hidden="true">&nbsp;&nbsp;</span>${esc(line.text)}</p>`;
    })
    .join("")}</div>`;
}

function diffHtml(diff) {
  const parts = diff.changes.map((change) => {
    const label = esc(t(FIELDS[change.field] || change.field));
    if (change.type === "text") {
      return `<h4>${label}</h4>${change.lines.length ? linesHtml(change.lines) : `<p class="sub">${esc(t("Empty"))}</p>`}`;
    }
    return `<p><strong>${label}:</strong> ${esc(value(change.field, change.before))} → ${esc(value(change.field, change.after))}</p>`;
  });
  if (!parts.length) parts.push(`<p class="sub">${esc(t("Nothing changed in this version."))}</p>`);
  let action = "";
  if (diff.latest && diff.previous_version) {
    action = `<button class="btn" type="button" data-revert="${diff.previous_version}">${esc(t("Undo this change"))}</button>`;
  } else if (!diff.latest) {
    action = `<button class="btn" type="button" data-revert="${diff.version}">${esc(t("Go back to this version"))}</button>`;
  }
  return `${parts.join("")}${action ? `<div class="kb-actions">${action}</div>` : ""}`;
}

export async function renderLog(view, { query, stale }) {
  const kind = query?.get("kind") || "";
  const target = query?.get("target") || "";
  const params = {};
  if (kind) params.kind = kind;
  if (target) params.target = target;
  const [logReply, archiveReply, clientReply] = await Promise.all([
    get("coach-log", params),
    get("knowledge/archive"),
    get("mcp_clients").catch(() => ({ data: { names: {} } })),
  ]);
  if (stale()) return;
  const names = clientReply.data?.names || {};
  const archive = archiveReply.data.articles || [];
  const body = coachShell(view, "log", {
    intro: t("Every change to what your coach knows and remembers. Open one to see what changed or to go back."),
  });
  const filter = target
    ? `<p>${esc(t("Changes to {name}", { name: target }))} · <a href="#/coach/log">${esc(t("Show everything"))}</a></p>`
    : `<div class="settings-field"><label for="log-filter">${esc(t("Show"))}</label>
        <select class="btn" id="log-filter">${FILTERS.map(([id, label]) => `<option value="${id}"${id === kind ? " selected" : ""}>${esc(t(label))}</option>`).join("")}</select></div>`;
  body.innerHTML = `
    <section class="card" aria-labelledby="log-title">
      <h2 id="log-title">${esc(t("Changes"))}</h2>
      ${filter}
      <p class="sub" data-log-status role="status" aria-live="polite" tabindex="-1"></p>
      <ol class="coach-log" data-log-list></ol>
      <button class="btn" type="button" data-log-more hidden>${esc(t("Show older changes"))}</button>
    </section>
    ${target ? "" : `<section class="card" id="coach-archive" aria-labelledby="archive-title">
      <h2 id="archive-title" tabindex="-1">${esc(t("Archive"))}</h2>
      <p class="sub">${esc(t("Archived articles are hidden from your coach pages. Their history stays here."))}</p>
      ${archive.length
        ? `<ul class="kb-list" data-archive-list>${archive.map((article) => `<li data-slug="${esc(article.slug)}" data-version="${article.version}">
            <a href="#/coach/knowledge/${encodeURIComponent(article.slug)}">${esc(article.title)}</a>
            <span class="sub">${esc(statusLabel(article.status))} · ${esc(userDateTime(article.updated_at))}</span>
            <button class="btn" type="button" data-restore-article>${esc(t("Bring back"))}</button></li>`).join("")}</ul>`
        : `<p class="empty">${esc(t("Nothing is archived."))}</p>`}
      <p class="sub" data-archive-status role="status" aria-live="polite"></p>
    </section>`}`;

  const list = body.querySelector("[data-log-list]");
  const more = body.querySelector("[data-log-more]");
  const status = body.querySelector("[data-log-status]");
  let next = null;
  const append = (data) => {
    list.insertAdjacentHTML("beforeend", data.entries.map((entry) => entryHtml(entry, names)).join(""));
    next = data.next;
    more.hidden = !next;
    if (!list.children.length) list.outerHTML = `<p class="empty">${esc(t("No changes yet."))}</p>`;
  };
  append(logReply.data);
  if (query?.get("view") === "archive") body.querySelector("#archive-title")?.focus();

  body.querySelector("#log-filter")?.addEventListener("change", (event) => {
    location.hash = event.target.value ? `#/coach/log?kind=${encodeURIComponent(event.target.value)}` : "#/coach/log";
  });
  more.addEventListener("click", async () => {
    more.disabled = true;
    try {
      const { data } = await get("coach-log", { ...params, before: next });
      if (stale() || !view.isConnected) return;
      const first = list.children.length;
      append(data);
      list.children[first]?.querySelector("summary")?.focus();
    } catch {
      status.textContent = t("This part could not load right now.");
    } finally {
      more.disabled = false;
    }
  });
  // A diff loads the first time its entry opens; the entry and focus stay where they are.
  list.addEventListener("toggle", (event) => {
    const details = event.target;
    if (!details.open || details.dataset.loaded) return;
    details.dataset.loaded = "1";
    loadDiff(details).catch(() => {
      delete details.dataset.loaded;
      details.querySelector("[data-diff]").innerHTML = `<p class="notice error" role="alert">${esc(t("This part could not load right now."))}</p>`;
    });
  }, true);
  list.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-revert]");
    if (!button) return;
    const details = button.closest("details");
    button.disabled = true;
    try {
      const { data } = await post("coach-log/revert", {
        kind: details.dataset.kind,
        target: details.dataset.target,
        version: Number(button.dataset.revert),
      });
      if (stale() || !view.isConnected) return;
      await renderLog(view, { query, stale });
      const note = view.querySelector("[data-log-status]");
      if (note) {
        note.textContent = t("Done. Version {version} is in use now; the change log keeps the rest.", { version: data.version });
        note.focus();
      }
    } catch {
      if (stale() || !view.isConnected) return;
      button.disabled = false;
      status.textContent = t("Couldn't save. Check your connection and try again.");
    }
  });
  body.querySelector("[data-archive-list]")?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-restore-article]");
    if (!button) return;
    const row = button.closest("li");
    const note = body.querySelector("[data-archive-status]");
    button.disabled = true;
    try {
      await post("knowledge/article/edit", {
        slug: row.dataset.slug,
        expected_version: Number(row.dataset.version),
        status: "active",
      });
      if (stale() || !view.isConnected) return;
      await renderLog(view, { query, stale });
      const fresh = view.querySelector("#archive-title");
      const freshNote = view.querySelector("[data-archive-status]");
      if (freshNote) freshNote.textContent = t("Back in use.");
      fresh?.focus();
    } catch {
      if (stale() || !view.isConnected) return;
      button.disabled = false;
      note.textContent = t("Couldn't save. Check your connection and try again.");
    }
  });
}

async function loadDiff(details) {
  const { data } = await get("coach-log/diff", {
    kind: details.dataset.kind,
    target: details.dataset.target,
    version: details.dataset.version,
  });
  if (!details.isConnected) return;
  details.querySelector("[data-diff]").innerHTML = diffHtml(data);
}
