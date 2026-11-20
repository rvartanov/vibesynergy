// The knowledge-base lint on Coach → Methods & rules (#96, #120): warnings about the text
// your AI coach follows (a model name, a retired name, a rule range that does not match the
// rules in use, a tool that does not exist, a threshold written as a number). The server
// sends one item per finding ({check, kind, ref, article, names, expected}); the text is
// built here so it follows the portal language. A warning only: you decide about the text.
import { esc } from "./format.js";
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

export function lintCard(warnings) {
  if (!warnings?.length) return "";
  const items = warnings.map((item) => {
    const text = esc(lintText(item));
    const link = item.article
      ? ` <a href="#/coach/methods/${encodeURIComponent(item.article)}">${esc(t("Open"))}</a>`
      : "";
    return `<li data-lint-check="${esc(item.check)}">${text}${link}</li>`;
  }).join("");
  return `<section class="card" aria-labelledby="methods-lint" data-kb-lint>
    <div class="notice warning">
      <h2 id="methods-lint">${esc(t("Check of the texts"))}</h2>
      <p>${esc(t("Your AI coach gets these warnings too. Change the text, or ask your AI coach to suggest a change."))}</p>
      <ul>${items}</ul>
    </div>
  </section>`;
}
