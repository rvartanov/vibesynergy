// Gear: shoes, bikes and other equipment in one overview, with how far each has gone
// against its lifetime, when it was last used, and your own settings per item (status,
// lifetime limit, note). Totals are the source's (Strava); edits save directly.
import { get, post } from "../api.js";
import { esc, isNum, longDate, number, sportName } from "../format.js";
import { N, t } from "../i18n.js";
import { bindAutosave } from "../settings_autosave.js";

const KINDS = [
  ["shoes", N("Running shoes")],
  ["bike", N("Bikes")],
  ["other", N("Other gear")],
];
const STATUSES = [
  ["active", N("In use")],
  ["spare", N("Spare")],
  ["retired", N("Retired")],
];

const WARNINGS = {
  near: N("Close to its lifetime: time to look for a new one."),
  past: N("Past its lifetime: replace it soon."),
};

function lifeHtml(item) {
  if (!isNum(item.life_km) || !isNum(item.total_km)) return "";
  const text = t("{km} of {life} km", { km: number(item.total_km), life: number(item.life_km) });
  return `<p class="gear-life"><meter min="0" max="${item.life_km}" low="${0.75 * item.life_km}" high="${0.9 * item.life_km}" optimum="0" value="${Math.min(item.total_km, item.life_km)}" aria-label="${esc(t("Lifetime used"))}">${esc(text)}</meter> <span>${esc(text)}</span></p>`;
}

const SERVICE_STATE = {
  due: ["warning", N("Due now")],
  soon: ["advice", N("Due soon")],
  ok: ["success", N("Fine")],
};

function serviceEvery(svc) {
  return [svc.every_km ? t("every {km} km", { km: number(svc.every_km) }) : "", svc.every_days ? t("every {n} days", { n: number(svc.every_days) }) : ""].filter(Boolean).join(" · ");
}

function servicesHtml(item, id) {
  const rows = (item.services || []).map((svc) => {
    const [tone, label] = SERVICE_STATE[svc.state] || ["", ""];
    const last = svc.last_done ? t("last {date}", { date: longDate(svc.last_done) }) : "";
    return `<li data-service="${esc(svc.id)}"><b>${esc(svc.what)}</b> ${label ? `<span class="chip ${tone}">${esc(t(label))}</span>` : ""}
      <span class="sub">${esc([serviceEvery(svc), last].filter(Boolean).join(" · "))}</span>
      <button class="btn small" type="button" data-service-done="${esc(svc.id)}">${esc(t("Done today"))}</button>
      <button class="btn small" type="button" data-service-remove="${esc(svc.id)}" aria-label="${esc(t("Remove {what}", { what: svc.what }))}">${esc(t("Remove"))}</button></li>`;
  }).join("");
  return `<fieldset class="gear-services"><legend>${esc(t("Maintenance"))}</legend>
    ${rows ? `<ul class="plain-services">${rows}</ul>` : `<p class="sub">${esc(t("No maintenance yet. Add one, for example a chain every 2000 km."))}</p>`}
    <div class="gear-service-add">
      <label for="${id}-svc-what">${esc(t("What"))}</label><input id="${id}-svc-what" name="svc_what" maxlength="80">
      <label for="${id}-svc-km">${esc(t("Every … km"))}</label><input id="${id}-svc-km" name="svc_km" type="number" inputmode="numeric" min="1" step="1">
      <label for="${id}-svc-days">${esc(t("Every … days"))}</label><input id="${id}-svc-days" name="svc_days" type="number" inputmode="numeric" min="1" step="1">
      <button class="btn" type="button" data-service-add>${esc(t("Add"))}</button>
      <p class="sub" role="status" aria-live="polite" data-service-status></p>
    </div></fieldset>`;
}

function itemHtml(item) {
  const facts = [];
  if (!isNum(item.life_km) && isNum(item.total_km)) facts.push(t("{km} km", { km: number(item.total_km) }));
  facts.push(item.last_used ? t("last used {date}", { date: longDate(item.last_used) }) : t("not used since VibeSynergy started"));
  const chips = [
    item.used_last_30_days ? `<span class="chip success">${esc(t("Used in the last 30 days"))}</span>` : "",
    item.status === "spare" ? `<span class="chip info">${esc(t("Spare"))}</span>` : "",
  ].join("");
  const warning = WARNINGS[item.warning_level] ? `<p class="chip warning">${esc(t(WARNINGS[item.warning_level]))}</p>` : "";
  const due = (item.services || []).filter((svc) => svc.state === "due" || svc.state === "soon")
    .map((svc) => `<p class="chip ${svc.state === "due" ? "warning" : "advice"}">${esc(t(svc.state === "due" ? "{what}: due now" : "{what}: due soon", { what: svc.what }))}</p>`).join("");
  const model = [item.brand, item.model].filter(Boolean).join(" ");
  const id = `gear-${esc(item.ref).replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  return `<li class="card gear-item" data-ref="${esc(item.ref)}" data-km="${isNum(item.total_km) ? item.total_km : ""}">
    <h3>${esc(item.name || model || item.ref)}</h3>
    ${model && model !== item.name ? `<p class="sub">${esc(model)}</p>` : ""}
    ${lifeHtml(item)}
    <p class="sub">${esc(facts.join(" · "))}</p>
    ${chips ? `<p>${chips}</p>` : ""}${warning}${due}
    ${item.note ? `<p>${esc(item.note)}</p>` : ""}
    <details class="collapsible"><summary>${esc(t("Your settings for this item"))}</summary>
      <div class="gear-form">
        <label for="${id}-status">${esc(t("Status"))}</label>
        <select class="btn" id="${id}-status" name="status">${STATUSES.map(([value, label]) => `<option value="${value}"${value === item.status ? " selected" : ""}>${esc(t(label))}</option>`).join("")}</select>
        <label for="${id}-life">${esc(t("Lifetime (km)"))}</label>
        <input id="${id}-life" name="life_km" type="number" inputmode="numeric" min="1" max="99999" step="1" value="${isNum(item.life_km) ? item.life_km : ""}">
        <label for="${id}-note">${esc(t("Note"))}</label>
        <textarea id="${id}-note" name="note" rows="2" maxlength="1000">${esc(item.note || "")}</textarea>
        <p class="sub" role="status" aria-live="polite" data-gear-status></p>
      </div>
      ${servicesHtml(item, id)}
    </details>
  </li>`;
}

function groupHtml(kind, label, items) {
  if (!items.length) return "";
  return `<section aria-labelledby="gear-${kind}"><h2 id="gear-${kind}">${esc(t(label))}</h2>
    <ul class="plain gear-list">${items.map(itemHtml).join("")}</ul></section>`;
}

export async function render(view, ctx) {
  const { stale } = ctx;
  document.title = `${t("Gear")} · VibeSynergy`;
  const { data } = await get("gear");
  if (stale()) return;
  const gear = data.gear || [];
  const inUse = gear.filter((g) => g.status !== "retired");
  const retired = gear.filter((g) => g.status === "retired");
  const groups = KINDS.map(([kind, label]) => groupHtml(kind, label, inUse.filter((g) => g.kind === kind))).join("");
  view.innerHTML = `<div class="page-head"><div><h1>${esc(t("Gear"))}</h1>
      <p class="sub">${esc(t("Your shoes and bikes: how far they have gone and when it's time for new ones. Totals come from Strava."))}</p></div></div>
    ${gear.length ? groups : `<p class="empty">${esc(t("No gear yet. Connect Strava to see your shoes and bikes."))}</p>`}
    ${retired.length ? `<details class="collapsible card"><summary>${esc(t("Retired ({n})", { n: retired.length }))}</summary><ul class="plain gear-list">${retired.map(itemHtml).join("")}</ul></details>` : ""}
    ${defaultsHtml(data, inUse)}`;
  bindDefaults(view);
  for (const card of view.querySelectorAll(".gear-item")) {
    bindItem(card);
    bindServices(card, view, ctx);
  }
}

// Default gear per sport: new activities of that sport without gear count on it.
function defaultsHtml(data, inUse) {
  const sports = data.sports || [];
  if (!sports.length || !inUse.length) return "";
  const chosen = Object.fromEntries((data.defaults || []).map((d) => [d.sport, d.ref]));
  const rows = sports.map((sport) => {
    const id = `gear-default-${sport}`;
    const options = [`<option value="">${esc(t("None"))}</option>`, ...inUse.map((g) => `<option value="${esc(g.ref)}"${chosen[sport] === g.ref ? " selected" : ""}>${esc(g.name || g.ref)}</option>`)].join("");
    return `<li><label for="${esc(id)}">${esc(sportName(sport))}</label> <select class="btn" id="${esc(id)}" data-gear-default="${esc(sport)}">${options}</select></li>`;
  }).join("");
  const set = (data.defaults || []).length;
  return `<details class="collapsible card"><summary>${esc(set ? t("Default gear per sport ({n} set)", { n: set }) : t("Default gear per sport"))}</summary>
    <p class="sub">${esc(t("Activities from today on without gear count on this item. You can still change it per activity."))}</p>
    <ul class="plain gear-defaults">${rows}</ul><p class="sub" role="status" data-gear-default-status></p></details>`;
}

function bindDefaults(view) {
  const status = view.querySelector("[data-gear-default-status]");
  for (const select of view.querySelectorAll("[data-gear-default]")) {
    select.addEventListener("change", async () => {
      try {
        await post("gear-default", { sport: select.dataset.gearDefault, ref: select.value || null });
        status.textContent = t("Saved.");
      } catch (error) { status.textContent = t("Not saved: {error}", { error: error.message }); }
    });
  }
}

function bindServices(card, view, ctx) {
  const km = Number(card.dataset.km) || null;
  const status = card.querySelector("[data-service-status]");
  const reload = () => render(view, ctx);
  card.querySelector("[data-service-add]")?.addEventListener("click", async () => {
    const what = card.querySelector("[name=svc_what]").value.trim();
    const everyKm = Number(card.querySelector("[name=svc_km]").value) || null;
    const everyDays = Number(card.querySelector("[name=svc_days]").value) || null;
    try {
      await post(`gear/${encodeURIComponent(card.dataset.ref)}/service`, { what, every_km: everyKm, every_days: everyDays, km_now: km });
      reload();
    } catch (error) {
      status.textContent = error.message;
    }
  });
  for (const button of card.querySelectorAll("[data-service-done]")) {
    button.addEventListener("click", async () => {
      await post(`gear/service/${encodeURIComponent(button.dataset.serviceDone)}/done`, { km_now: km }).catch((error) => { status.textContent = error.message; });
      reload();
    });
  }
  for (const button of card.querySelectorAll("[data-service-remove]")) {
    button.addEventListener("click", async () => {
      await post(`gear/service/${encodeURIComponent(button.dataset.serviceRemove)}/remove`, {}).catch((error) => { status.textContent = error.message; });
      reload();
    });
  }
}

function bindItem(card) {
  const form = card.querySelector(".gear-form");
  const value = (name) => form.querySelector(`[name="${name}"]`).value.trim();
  bindAutosave(form, {
    statusEl: form.querySelector("[data-gear-status]"),
    save: () => {
      const life = Number(value("life_km"));
      return post(`gear/${encodeURIComponent(card.dataset.ref)}`, {
        status: value("status"),
        life_km: Number.isFinite(life) && life > 0 ? life : null,
        note: value("note") || null,
      });
    },
  });
}
