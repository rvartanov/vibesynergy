// Health → Complaints & injuries: what hurts now, how the pain went, and what the
// warnings say, then what healed. Logging today's pain saves at once; a new complaint is
// added with + (quick add) or on the start page.
import { get, post } from "../api.js";
import { esc, isNum, longDate, number, shortDate } from "../format.js";
import { N, language, t } from "../i18n.js";

const KINDS = { niggle: N("Niggle"), injury: N("Injury"), illness: N("Illness") };

function painLogHtml(log) {
  if (!log?.length) return `<p class="sub">${esc(t("No pain logged yet."))}</p>`;
  const rows = [...log].reverse().slice(0, 21).map((e) => `<tr><th scope="row">${esc(shortDate(e.date))}</th>
    <td class="num">${isNum(e.pain) ? `${esc(number(e.pain))}/10` : "—"}</td>
    <td>${e.affects_movement ? esc(t("changes how you move")) : ""}</td><td>${esc(e.note || "")}</td></tr>`).join("");
  return `<div class="table-wrap"><table aria-label="${esc(t("Pain log"))}"><thead><tr><th scope="col">${esc(t("Day"))}</th><th scope="col" class="num">${esc(t("Pain"))}</th><th scope="col"><span class="visually-hidden">${esc(t("Movement"))}</span></th><th scope="col">${esc(t("Note"))}</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function injuryHtml(item, { canWrite }) {
  const pain = isNum(item.latest_pain) ? t("pain {n}/10", { n: number(item.latest_pain) }) : t("no pain logged");
  const since = t("since {date}", { date: longDate(item.onset) });
  const kind = t(KINDS[item.kind] || item.kind || "");
  const flags = (item.flags || []).map((f) => `<p class="notice warning">${esc(f.message)}</p>`).join("");
  const options = Array.from({ length: 11 }, (_, n) => `<option value="${n}">${n}</option>`).join("");
  const log = canWrite && item.status !== "resolved"
    ? `<label class="sub">${esc(t("Pain today"))} <select class="btn" data-pain="${esc(item.id)}"><option value="">–</option>${options}</select></label> <span class="sub" role="status" aria-live="polite" data-pain-status></span>`
    : "";
  const resolved = item.resolved_on ? ` · ${esc(t("healed on {date}", { date: longDate(item.resolved_on) }))}` : "";
  return `<li class="card injury-item">
    <h3>${esc(item.title)}</h3>
    <p class="sub">${esc([kind, since].filter(Boolean).join(" · "))}${resolved}</p>
    <p><strong>${esc(pain)}</strong>${isNum(item.worst_pain) ? ` <span class="sub">${esc(t("worst {n}/10", { n: number(item.worst_pain) }))}</span>` : ""}</p>
    ${flags}${log}
    <details class="collapsible"><summary>${esc(t("Pain log ({n})", { n: (item.pain_log || []).length }))}</summary>${painLogHtml(item.pain_log)}
      ${item.notes ? `<p class="sub">${esc(item.notes)}</p>` : ""}</details>
  </li>`;
}

export async function render(view, { meta, stale }) {
  document.title = `${t("Complaints & injuries")} · VibeSynergy`;
  const { data } = await get("injuries", { lang: language() });
  if (stale()) return;
  const share = meta?.data?.share;
  const canWrite = !share || share.can_write;
  const open = data.injuries.filter((i) => i.status !== "resolved");
  const healed = data.injuries.filter((i) => i.status === "resolved");
  view.innerHTML = `<div class="page-head"><div><h1>${esc(t("Complaints & injuries"))}</h1>
      <p class="sub">${esc(t("What hurts now and how it goes. Add a new complaint with + or on the start page."))}</p></div></div>
    <section aria-labelledby="injuries-open"><h2 id="injuries-open">${esc(t("Now ({n})", { n: open.length }))}</h2>
      ${open.length ? `<ul class="gear-list">${open.map((i) => injuryHtml(i, { canWrite })).join("")}</ul>` : `<p class="empty">${esc(t("Nothing hurts. Good."))}</p>`}</section>
    ${healed.length ? `<details class="card collapsible"><summary>${esc(t("Healed ({n})", { n: healed.length }))}</summary><ul class="gear-list">${healed.map((i) => injuryHtml(i, { canWrite: false })).join("")}</ul></details>` : ""}`;
  for (const select of view.querySelectorAll("[data-pain]")) {
    select.addEventListener("change", async () => {
      if (select.value === "") return;
      const status = select.parentElement.parentElement.querySelector("[data-pain-status]");
      try {
        await post("diary/injury", { injury_id: select.dataset.pain, pain: Number(select.value) });
        if (stale()) return;
        status.textContent = t("Saved.");
      } catch (error) {
        status.textContent = error.message;
      }
    });
  }
}
