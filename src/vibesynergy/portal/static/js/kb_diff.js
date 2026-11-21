// A readable line diff (the server's query/text_diff.py): changed lines with a little
// context, long unchanged runs folded. Coach → Change log shows one version against the one
// before; a knowledge-base proposal shows what it would change instead of the whole article
// (#137).
import { esc } from "./format.js";
import { t } from "./i18n.js";

export function linesHtml(lines) {
  return `<div class="coach-diff">${(lines || [])
    .map((line) => {
      if (line.op === "gap") return `<p class="coach-diff-gap">${esc(t("… {count} lines unchanged", { count: line.text }))}</p>`;
      if (line.op === "add") return `<ins><span class="visually-hidden">${esc(t("Added:"))} </span><span aria-hidden="true">+ </span>${esc(line.text)}</ins>`;
      if (line.op === "del") return `<del><span class="visually-hidden">${esc(t("Removed:"))} </span><span aria-hidden="true">− </span>${esc(line.text)}</del>`;
      return `<p class="coach-diff-same"><span aria-hidden="true">&nbsp;&nbsp;</span>${esc(line.text)}</p>`;
    })
    .join("")}</div>`;
}
