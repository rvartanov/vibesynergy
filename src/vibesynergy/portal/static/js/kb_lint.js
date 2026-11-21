// The knowledge-base lint on Coach → Methods & rules (#96, #120): warnings about the text
// your AI coach follows (a model name, a retired name, a rule range that does not match the
// rules in use, a tool that does not exist, a threshold written as a number). The server
// sends one item per finding ({check, kind, ref, article, names, expected}); the text is
// built here so it follows the portal language. A warning only: you decide about the text.
//
// Below the warnings: what VibeSynergy fixed itself without asking (#137). Only outdated
// names, rule ranges and thresholds now read live; each fix is a version in the change log
// and is undone with one click (the change log's revert).
import { post } from "./api.js";
import { esc, userDateTime } from "./format.js";
import { N, t } from "./i18n.js";

const MESSAGES = {
  model_name: N("{ref} names a model or provider: {names}. Write “AI coach” instead, so any model can follow it."),
  retired_name: N("{ref} uses a name that is no longer in use: {names}."),
  rule_range: N("{ref} refers to {names}, but the rules in use are {expected}."),
  unknown_rule: N("{ref} refers to {names}, which is not a rule in use."),
  unknown_tool: N("{ref} names a tool that does not exist: {names}."),
  fixed_threshold: N("{ref} has thresholds as fixed numbers: {names}. They change after every test; your AI coach should read them live (Training → Zones & thresholds)."),
};

export function lintText(item) {
  const template = MESSAGES[item.check];
  const names = (item.names || []).join(", ");
  if (!template) return `${item.ref}: ${names}`;
  return t(template, { ref: item.ref, names, expected: (item.expected || []).join(", ") });
}

const FIX_KINDS = {
  retired_name: N("outdated names"),
  unknown_tool: N("tool names"),
  rule_range: N("rule ranges"),
  fixed_threshold: N("thresholds now read live"),
};

/** "outdated names, rule ranges": what kind of fixes, in the server's order. */
export function fixKinds(checks) {
  return (checks || []).map((check) => t(FIX_KINDS[check] || check)).join(", ");
}

function logHref(item) {
  return `#/coach/log?kind=${encodeURIComponent(item.kind)}&amp;target=${encodeURIComponent(item.target)}`;
}

function editorialItem(item, canUndo) {
  const fixes = (item.fixes || []).map((fix) => `${fix.found} → ${fix.fixed}`).join(", ");
  let action = "";
  if (item.state === "current" && canUndo) {
    action = `<button class="btn" type="button" data-editorial-undo data-kind="${esc(item.kind)}" data-target="${esc(item.target)}" data-version="${esc(String(item.base_version))}">${esc(t("Undo"))}</button>`;
  } else if (item.state !== "current") {
    action = `<span class="sub">${esc(item.state === "undone" ? t("Undone") : t("Changed since"))}</span>`;
  }
  return `<li data-editorial="${esc(item.id)}"><strong>${esc(item.title)}</strong>: ${esc(fixes)}
    <span class="sub">· ${esc(userDateTime(item.at))}</span>
    <span class="kb-actions">${action} <a href="${logHref(item)}">${esc(t("See the change"))}</a></span>
    <span class="sub" data-editorial-status role="status" aria-live="polite"></span></li>`;
}

export function lintCard(warnings, editorial = [], { canUndo = true } = {}) {
  if (!warnings?.length && !editorial?.length) return "";
  const items = (warnings || []).map((item) => {
    const text = esc(lintText(item));
    const link = item.article
      ? ` <a href="#/coach/methods/${encodeURIComponent(item.article)}">${esc(t("Open"))}</a>`
      : "";
    return `<li data-lint-check="${esc(item.check)}">${text}${link}</li>`;
  }).join("");
  const open = items
    ? `<div class="notice warning">
      <p>${esc(t("Your AI coach gets these warnings too. Change the text, or ask your AI coach to suggest a change."))}</p>
      <ul>${items}</ul>
    </div>`
    : "";
  const fixed = editorial?.length
    ? `<h3 id="methods-lint-fixed">${esc(t("Fixed without asking you"))}</h3>
      <p class="sub">${esc(t("Only outdated names, rule ranges and thresholds that are now read live. Nothing else in the text changed. Each fix is in the change log and can be undone."))}</p>
      <ul class="kb-editorial" data-kb-editorial>${editorial.map((item) => editorialItem(item, canUndo)).join("")}</ul>`
    : "";
  return `<section class="card" aria-labelledby="methods-lint" data-kb-lint>
    <h2 id="methods-lint">${esc(t("Check of the texts"))}</h2>
    ${open}${fixed}
  </section>`;
}

/** One click undoes a fix: the change log brings the version before it back. */
export function bindEditorial(root, refresh) {
  for (const button of root.querySelectorAll("[data-editorial-undo]")) {
    button.addEventListener("click", async () => {
      const status = button.closest("li").querySelector("[data-editorial-status]");
      button.disabled = true;
      try {
        await post("coach-log/revert", {
          kind: button.dataset.kind,
          target: button.dataset.target,
          version: Number(button.dataset.version),
        });
        await refresh();
      } catch {
        button.disabled = false;
        status.textContent = t("Couldn't save. Check your connection and try again.");
      }
    });
  }
}
