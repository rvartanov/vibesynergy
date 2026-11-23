// The workout's gear on its page (#114): one tap links the gear VibeSynergy thinks you used,
// with the reason in words; a small picker takes other gear, several swim items or none.
// Only on your own portal: a shared page never loads this (the API routes are not shared).
//
// With "Keep name, texts and gear the same on Strava and Garmin" on, the gear of a published workout goes to
// Strava (one bike or pair of shoes) and to the Garmin items you mapped on the Gear page;
// swim gear and other gear without a slot there stays in VibeSynergy.
import { get, post } from "./api.js";
import { esc, userDate, userDateTime } from "./format.js";
import { N, t } from "./i18n.js";

const SOURCE = { vs: "VibeSynergy", strava: "Strava", garmin: "Garmin" };
const KIND = { edit: N("Changed"), taken: N("Taken over") };

function names(items) {
  return items.map((i) => i.name).join(", ");
}

// Why this gear is suggested, in words.
function reasonText(s) {
  const what = names([s.main, ...s.extras].filter(Boolean));
  if (s.reason === "last") return t("Suggested: {gear}, the gear of your last workout of this sport ({date}).", { gear: what, date: userDate(s.date) });
  if (s.reason === "default") return t("Suggested: {gear}, your default gear for this sport.", { gear: what });
  if (s.main?.kind === "bike") return t("Suggested: {gear}, the bike you used last.", { gear: what });
  return t("Suggested: {gear}, the shoes you used last.", { gear: what });
}

function linkedText(d) {
  const all = [d.main, ...d.extras].filter(Boolean);
  if (!all.length) return d.how === "own" ? t("No gear: you said so.") : t("No gear linked yet.");
  if (d.how === "source" && !d.extras.length) return t("From Strava: {gear}.", { gear: names(all) });
  if (d.how === "default" && !d.extras.length) return t("Your default for this sport: {gear}.", { gear: names(all) });
  return t("Linked: {gear}.", { gear: names(all) });
}

// Where the linked gear goes, honestly: per item, and only once the sync is on.
function whereText(d) {
  const all = [d.main, ...d.extras].filter(Boolean);
  const sync = d.sync || {};
  if (!sync.enabled) return t("Your gear stays in VibeSynergy. To keep it the same on Strava and Garmin, switch that on in Settings, with the name and texts.");
  if (sync.state === "refused") return t("Strava or Garmin did not take this gear. Pick other gear, or change it there.");
  if (!sync.published) return t("Once you publish this workout, its gear goes to Strava and Garmin too.");
  if (!all.length) return t("Strava and Garmin follow when you link gear.");
  const place = (i) => (i.goes_to.length === 2 ? t("Strava and Garmin") : i.goes_to[0] === "strava" ? "Strava" : i.goes_to[0] === "garmin" ? "Garmin" : t("only in VibeSynergy"));
  return all.map((i) => `${i.name}: ${place(i)}`).join(" · ");
}

function historyHtml(rows, tz) {
  if (!rows.length) return "";
  const items = rows.map((h) => {
    const label = h.kind === "overwritten"
      ? t("Overwritten, from {source}", { source: SOURCE[h.source] || h.source })
      : `${SOURCE[h.source] || h.source} · ${t(KIND[h.kind] || h.kind)}`;
    const gear = h.names.length ? esc(h.names.join(", ")) : `<span class="muted">${esc(t("No gear"))}</span>`;
    return `<li><div class="sub">${esc(label)} · <time datetime="${esc(h.at)}">${esc(userDateTime(h.at, tz))}</time></div><div>${gear}</div></li>`;
  }).join("");
  return `<details class="wt-history"><summary>${esc(t("History ({n})", { n: rows.length }))}</summary><ol class="wt-versions">${items}</ol></details>`;
}

function pickerHtml(d) {
  const chosen = d.how === "own" ? (d.main?.ref || "none") : "auto";
  const option = (value, label) => `<option value="${esc(value)}"${value === chosen ? " selected" : ""}>${esc(label)}</option>`;
  const auto = d.how && d.how !== "own" && d.main ? t("Automatic: {name}", { name: d.main.name }) : t("Automatic");
  const extras = new Set(d.extras.map((e) => e.ref));
  const swim = d.options.extras.length
    ? `<fieldset class="wg-extras"><legend>${t("Extra gear")}</legend>
        ${d.options.extras.map((o) => `<label class="check"><input type="checkbox" name="wg-extra" value="${esc(o.ref)}"${extras.has(o.ref) ? " checked" : ""}> ${esc(o.name)}</label>`).join("")}
      </fieldset>`
    : "";
  return `<details class="wg-picker" data-wg-picker><summary>${t("Other gear or none")}</summary>
    <label for="wg-main">${t("Bike or shoes")}</label>
    <select class="btn" id="wg-main" name="wg-main">
      ${option("auto", auto)}
      ${d.options.main.map((o) => option(o.ref, o.name)).join("")}
      ${option("none", t("No gear"))}
    </select>
    ${swim}
    <div class="ql-actions"><button class="btn" type="button" data-wg-save>${t("Save gear")}</button></div>
    <p class="sub"><a href="#/gear">${t("Add gear of your own, such as a pull buoy, on the Gear page")}</a></p>
  </details>`;
}

export function gearHtml(d, tz) {
  const s = d.suggestion;
  const offer = s && !s.linked && (s.main || s.extras.length)
    ? `<p class="sub" id="wg-reason">${esc(reasonText(s))}</p>
       <div class="ql-actions"><button class="btn primary" type="button" data-wg-link aria-describedby="wg-reason">${esc(t("Link {gear}", { gear: names([s.main, ...s.extras].filter(Boolean)) }))}</button></div>`
    : "";
  return `<h2 id="wg-title">${t("Gear")}</h2>
    <p data-wg-linked tabindex="-1">${esc(linkedText(d))}</p>
    ${offer}
    ${pickerHtml(d)}
    <p class="sub" id="wg-where">${esc(whereText(d))}</p>
    <p class="sub" role="status" data-wg-status></p>
    ${historyHtml(d.history, tz)}`;
}

// Mount the card into `el`.
export async function workoutGear(el, id, { stale, tz }) {
  if (!el) return;
  let data;
  try {
    data = (await get("workout-gear", { id })).data;
  } catch (error) {
    if (!stale() && el.isConnected) {
      el.innerHTML = `<p class="sub error-text">${esc(error.message)}</p>`;
      el.hidden = false;
    }
    return;
  }
  if (stale() || !el.isConnected) return;

  const draw = (message, focus) => {
    const open = el.querySelector("[data-wg-picker]")?.open;
    el.innerHTML = gearHtml(data, tz);
    el.hidden = false;
    if (open) el.querySelector("[data-wg-picker]").open = true;
    bind();
    if (message) el.querySelector("[data-wg-status]").textContent = message;
    if (focus) (el.querySelector(focus) || el.querySelector("[data-wg-linked]"))?.focus();
  };

  const save = async (body, focus) => {
    const status = el.querySelector("[data-wg-status]");
    status.classList.remove("error-text");
    status.textContent = t("Saving…");
    try {
      const reply = await post("workout-gear", { id, ...body });
      if (stale() || !el.isConnected) return;
      data = reply.data;
      draw(t("Saved."), focus);
    } catch (error) {
      status.classList.add("error-text");
      status.textContent = t("Not saved: {error}", { error: error.message });
    }
  };

  const bind = () => {
    const link = el.querySelector("[data-wg-link]");
    link?.addEventListener("click", () => {
      const s = data.suggestion;
      // Keep the swim gear you already linked when the suggestion is only the main item.
      const extras = s.extras.length ? s.extras.map((e) => e.ref) : data.extras.map((e) => e.ref);
      save({ main: s.main ? s.main.ref : data.how === "own" ? (data.main?.ref || "none") : null, extras }, "[data-wg-linked]");
    });
    el.querySelector("[data-wg-save]")?.addEventListener("click", () => {
      const main = el.querySelector("[name=wg-main]").value;
      const extras = [...el.querySelectorAll("[name=wg-extra]:checked")].map((box) => box.value);
      save({ main: main === "auto" ? null : main, extras }, "[data-wg-save]");
    });
  };

  draw();
}
