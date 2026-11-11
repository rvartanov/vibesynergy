// Training → Fitness: zones and thresholds, form, and best efforts, as tabs (page links
// with aria-current, like Settings). The tab name is the h1, "Fitness" the caption.
import { esc } from "./format.js";
import { N, t } from "./i18n.js";

export const FITNESS_TABS = [
  ["", N("Zones & thresholds")],
  ["form", N("Form")],
  ["bests", N("Best efforts")],
];

export function fitnessHeadHtml(current, intro = "") {
  const label = (FITNESS_TABS.find(([id]) => id === current) || FITNESS_TABS[0])[1];
  const tabs = FITNESS_TABS.map(
    ([id, name]) => `<a class="btn" href="#/zones${id ? `/${id}` : ""}"${id === current ? ' aria-current="page"' : ""}>${esc(t(name))}</a>`,
  ).join("");
  return `<div class="page-head settings-head"><div><p class="settings-caption">${esc(t("Fitness"))}</p><h1>${esc(t(label))}</h1>${intro ? `<div class="sub">${esc(intro)}</div>` : ""}</div></div>
    <nav class="tabs settings-nav" aria-label="${esc(t("Fitness"))}">${tabs}</nav>`;
}
