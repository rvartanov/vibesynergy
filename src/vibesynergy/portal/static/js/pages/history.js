// History (#/history): per source two ways to fill in the past, with an estimate and a
// recommendation: over its API (VibeSynergy fetches month by month in the background; never for
// Strava) or from its account export (request it once, import in minutes). Below, the runs. The run list
// refreshes itself while something runs; the panels are left alone, so a half-filled form or
// an upload in progress survives the refresh.
import { get, post } from "../api.js";
import { esc, hoursMinutes, monthYear, number, sourceName, userDateTime, weekday } from "../format.js";
import { N, t } from "../i18n.js";
import { bindPeriodPicker, periodPickerHtml, resolvePeriod } from "../period_picker.js";
import { filesHtml } from "./data.js";

// The page: filling each source's history, and importing files (activity files and the
// drop folder).
export async function render(view, { meta, stale }, { embedded = false } = {}) {
  const share = meta.data.share;
  const canWrite = !share || share.can_write;
  if (!embedded) document.title = `${t("History")} · VibeSynergy`;
  const titleTag = embedded ? "h2" : "h1";
  view.innerHTML = `
    <div class="page-head"><div><${titleTag}>${t("History")}</${titleTag}><div class="sub">${t("Fill in the past from each source, import account exports and activity files.")}</div></div></div>
    <section class="card history-card"><div class="loading" role="status">${t("Loading…")}</div></section>
    ${canWrite ? `<section class="card files-card"><div class="loading" role="status">${t("Loading…")}</div></section>` : ""}`;
  const files = view.querySelector(".files-card");
  if (files) filesHtml(files, stale);
  await historyCard(view.querySelector(".history-card"), { stale, canWrite, today: meta.data.today });
}

const STATUS = {
  running: ["good", N("running")],
  waiting: ["warning", N("waiting for the API limit")],
  paused: ["", N("paused")],
  done: ["", N("done")],
  stuck: ["critical", N("stuck")],
  cancelled: ["", N("cancelled")],
  failed: ["critical", N("failed")],
};
const ACTIVE = ["running", "waiting", "paused"];
const REFRESH_MS = 10_000;
const SHOWN = 8;
const CHUNK = 8 * 1024 * 1024;
const MAX_TRIES = 8;

export async function historyCard(el, ctx) {
  if (!el) return;
  let reply;
  try {
    reply = await get("history");
  } catch (error) {
    el.innerHTML = `<h2>${t("History")}</h2><div class="notice error">${esc(error.message)}</div>`;
    return;
  }
  if (ctx.stale() || !el.isConnected) return;
  const d = reply.data;
  el.innerHTML = `<h2>${t("History")}</h2>
    <p class="sub" style="margin-top:0">${t("Fill in the past with an account export: request it once, importing takes minutes. Strava works only this way.")}</p>
    <div class="flash" role="status"></div>
    ${ctx.canWrite && d.sources.length ? `<div class="fill-sources">${d.sources.map((s) => sourceHtml(s)).join("")}</div>` : ""}
    <h3>${t("Runs")}</h3>
    <ul class="fills"></ul>`;
  const say = (text, error = false) => {
    el.querySelector(".flash").innerHTML = `<div class="notice${error ? " error" : ""}">${esc(text)}</div>`;
  };
  const runs = el.querySelector(".fills");
  let timer = null;
  const refresh = async (fills) => {
    clearTimeout(timer);
    if (!fills) {
      try {
        fills = (await get("history")).data.fills;
      } catch (error) {
        say(error.message, true);
        return;
      }
    }
    if (ctx.stale() || !el.isConnected) return;
    runs.innerHTML = fills.length ? fills.slice(0, SHOWN).map((f) => fillHtml(f, ctx.canWrite)).join("") : `<li class="empty">${t("Nothing filled yet.")}</li>`;
    for (const panel of el.querySelectorAll(".fill-source")) {
      const s = d.sources.find((x) => x.source === panel.dataset.source);
      panel.querySelector(".fill-state").textContent = stateText(s, fills);
    }
    if (fills.some((f) => f.status === "running" || f.status === "waiting")) timer = setTimeout(() => refresh(), REFRESH_MS);
  };
  refresh(d.fills);

  runs.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    button.disabled = true;
    try {
      await post(`history/${button.dataset.fill}/${button.dataset.action}`);
      await refresh();
    } catch (error) {
      say(error.message, true);
      button.disabled = false;
    }
  });
  if (!ctx.canWrite) return;

  for (const panel of el.querySelectorAll(".fill-source")) {
    const s = d.sources.find((x) => x.source === panel.dataset.source);
    const open = panel.querySelector(".fill-open");
    panel.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-route]");
      if (!button) return;
      for (const b of panel.querySelectorAll("button[data-route]")) b.classList.toggle("selected", b === button);
      if (button.dataset.route === "api") openApi(open, s, ctx, say, refresh);
      else openExport(open, s, refresh);
    });
  }
}

// One source: the two routes with what each costs, the recommended one marked.
function sourceHtml(s) {
  const recommended = `<span class="chip good">${t("recommended")}</span>`;
  const api = s.api_off ? "" : `<div class="fill-route">
      <div><b>${t("Over the API")}</b> ${s.recommend === "api" ? recommended : ""}</div>
      <div class="sub">${esc(apiText(s))}</div>
      <button class="btn" type="button" data-route="api">${t("Fill over the API…")}</button>
    </div>`;
  const exp = s.export
    ? `<div class="fill-route">
      <div><b>${t("From the export")}</b> ${s.recommend === "export" ? recommended : ""}</div>
      <div class="sub">${t("Everything at once. Requesting it takes a few hours to a day; importing it then takes minutes.")}</div>
      <button class="btn" type="button" data-route="export">${t("Import the export…")}</button>
    </div>`
    : "";
  return `<section class="fill-source" data-source="${esc(s.source)}">
    <h3>${esc(sourceName(s.source))}</h3>
    <div class="sub fill-state"></div>
    <div class="fill-routes">${api}${exp}</div>
    <div class="fill-open"></div>
  </section>`;
}

function apiText(s) {
  const period = s.period ? t("{from} to {until}", { from: month(s.period.start), until: month(s.period.end) }) : null;
  if (s.per_year_hours !== null) {
    const rate = t("about {time} per year of history (one request per second, per day and dataset)", { time: hoursMinutes(s.per_year_hours * 3600) });
    return period
      ? t("About {time} for {period}: {rate}.", { time: hoursMinutes(s.hours * 3600), period, rate })
      : t("Already synced as far back as it goes; an earlier period takes {rate}.", { rate });
  }
  return period ? t("Suggested: {period}.", { period }) : t("Already synced as far back as it goes.");
}

// The latest run of this source, either route.
function stateText(s, fills) {
  const archive = s.export?.archive;
  const latest = fills.find((f) => f.source === s.source || (archive && f.source === archive));
  if (!latest) return t("Nothing filled yet.");
  const route = latest.method === "api" ? t("over the API, {from} to {until}", { from: month(latest.start), until: month(latest.end) }) : t("from the export");
  const [, word] = STATUS[latest.status] || ["", latest.status];
  return t("Last run: {route} ({status}).", { route, status: t(word) });
}

function openApi(open, s, ctx, say, refresh) {
  const today = ctx.today;
  const fillValue = s.period?.start && s.period?.end
    ? { start: s.period.start, end: s.period.end }
    : { period: "custom", start: "", end: "" };
  const fillId = `fill-${s.source}`;
  open.innerHTML = `<form class="fill-start filters">
      ${periodPickerHtml({ id: fillId, value: fillValue, today, max: today, applyButton: false })}
      <label><span>&nbsp;</span><button class="btn primary" type="submit">${t("Fill history")}</button></label>
    </form>
    <p class="sub">${esc(s.period ? t("Suggested: {n} months, from when the account started up to what the regular syncs already have.", { n: s.period.months }) : t("Choose the period."))} ${t("It runs month by month after the regular syncs; progress shows under Runs.")}</p>`;
  const form = open.querySelector("form");
  const fillPicker = bindPeriodPicker(open, {
    id: fillId,
    value: fillValue,
    today,
    max: today,
    applyButton: false,
    onChange: () => {},
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button");
    const message = fillPicker.validate();
    if (message) return;
    const { start, end } = resolvePeriod(fillPicker.value(), today);
    button.disabled = true;
    try {
      const fill = (await post("history/start", { source: s.source, start, end })).data;
      say(t("Filling {source}: {n} months, oldest first, after the regular syncs.", { source: sourceName(fill.source), n: fill.total }));
      open.innerHTML = "";
      await refresh();
    } catch (error) {
      say(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
}

function openExport(open, s, refresh) {
  open.innerHTML = `<p class="sub">${esc(t(s.export.steps))} <a href="${esc(s.export.url)}" target="_blank" rel="noopener">${t("Request it")}</a>.</p>
    <p class="sub">${t("Then upload the zip as you received it. If the upload is interrupted, choose the same file again and it continues where it stopped. You can also put the zip in the drop folder on this server.")}</p>
    <label class="drop-zone archive-zone"><input type="file" accept=".zip,.ZIP" hidden>
      <span>${t("Drop the export here or")} <u>${t("choose the zip")}</u></span></label>
    <div class="fill-bar upload-bar" hidden><i></i></div>
    <div class="sub" data-archive-status></div>`;
  const zone = open.querySelector(".archive-zone");
  const status = open.querySelector("[data-archive-status]");
  const bar = open.querySelector(".upload-bar");
  const send = async (file) => {
    if (!file) return;
    if (!/\.zip$/i.test(file.name)) {
      status.textContent = t("Choose the export's .zip file.");
      return;
    }
    zone.classList.add("busy");
    bar.hidden = false;
    try {
      const fill = await uploadResumable(file, (received, total, note) => {
        bar.firstElementChild.style.width = `${Math.round((received / total) * 100)}%`;
        status.textContent = note || t("Uploading {file}: {pct}% ({done} of {total})", { file: file.name, pct: Math.round((received / total) * 100), done: mb(received), total: mb(total) });
      });
      status.textContent = t("{file} uploaded; the {what} is being imported in the background.", { file: file.name, what: t(fill.name) });
      await refresh();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      zone.classList.remove("busy");
    }
  };
  zone.querySelector("input").addEventListener("change", (event) => send(event.target.files[0]));
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("over");
    send(event.dataTransfer.files[0]);
  });
}

// Upload in chunks, each at the offset the server has; after a dropped connection it asks
// where the upload is and continues. The upload id is remembered per file in this browser,
// so choosing the same file again (after a reload) continues too.
// `expect` (garmin_export or strava_export) makes the server refuse a zip of another kind.
export async function uploadResumable(file, progress, { expect } = {}) {
  const key = `vibesynergy-upload:${file.name}:${file.size}:${file.lastModified}`;
  let id = remembered(key);
  let received = 0;
  if (id) {
    try {
      received = (await get(`history/uploads/${id}`)).data.received;
    } catch {
      id = null; // finished, cancelled or expired: start again
    }
  }
  if (!id) {
    id = (await post("history/uploads", { name: file.name, size: file.size })).data.id;
    remember(key, id);
  }
  progress(received, file.size);
  let tries = 0;
  while (received < file.size) {
    let response;
    try {
      response = await fetch(`api/history/uploads/${id}?offset=${received}`, {
        method: "POST",
        headers: { "x-vibesynergy": "portal", "content-type": "application/octet-stream" },
        body: file.slice(received, received + CHUNK),
      });
    } catch {
      response = null; // the connection dropped
    }
    const body = response ? await response.json().catch(() => ({})) : {};
    if (response?.ok) {
      received = body.data.received;
      tries = 0;
      progress(received, file.size);
      continue;
    }
    if (response?.status === 409) {
      received = body.received; // the server has more (or less) than we thought
      continue;
    }
    if (response && response.status < 500) {
      forget(key);
      throw new Error(body.error || `${response.status} ${response.statusText}`);
    }
    tries += 1;
    if (tries > MAX_TRIES) throw new Error(t("The connection keeps dropping. Choose the same file again later: it continues where it stopped."));
    const wait = Math.min(30, 2 ** tries);
    progress(received, file.size, t("Connection lost at {done}; trying again in {n} s…", { done: mb(received), n: wait }));
    await new Promise((resolve) => setTimeout(resolve, wait * 1000));
    try {
      received = (await get(`history/uploads/${id}`)).data.received;
    } catch {
      // still offline: the next try tells
    }
  }
  let fill;
  try {
    fill = (await post(`history/uploads/${id}/finish`, expect ? { expect } : {})).data;
  } catch (error) {
    // Refused (say, the wrong export): the server dropped the upload, so start afresh next
    // time. A dropped connection leaves it there, and choosing the file again finishes it.
    const gone = await get(`history/uploads/${id}`).then(() => false, () => true);
    if (gone) forget(key);
    throw error;
  }
  forget(key);
  return fill;
}

function remembered(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function remember(key, id) {
  try {
    localStorage.setItem(key, id);
  } catch {
    // private window: an interrupted upload then starts again
  }
}

function forget(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    // nothing to forget
  }
}

function fillHtml(f, canWrite) {
  const [tone, word] = STATUS[f.status] || ["", f.status];
  const what = f.method === "api" ? `${sourceName(f.source)}, ${t("{from} to {until}", { from: month(f.start), until: month(f.end) })}` : `${t(f.name)}: ${(f.file || "").split("/").pop()}`;
  const facts = [];
  if (f.total && f.status !== "done") {
    // API runs count months; the Strava archive counts files; the Garmin export a share.
    if (f.method === "api") facts.push(t("{done} of {total} months", { done: number(f.done), total: number(f.total) }));
    else if (f.source === "strava_export") facts.push(t("{done} of {total} files", { done: number(f.done), total: number(f.total) }));
    else facts.push(t("{pct}% done", { pct: Math.round((f.progress || 0) * 100) }));
  }
  // The ETA counts fetching time only; waits for a daily API limit come on top.
  if (f.eta_s) facts.push(f.status === "waiting" ? t("about {time} of fetching left, plus the waits for the API limit", { time: hoursMinutes(f.eta_s) }) : t("about {time} of fetching left", { time: hoursMinutes(f.eta_s) }));
  if (f.status === "waiting" && f.wait_until) facts.push(t("continues {when}", { when: when(f.wait_until) }));
  if (f.partial?.length) facts.push(t("partial: {months}", { months: f.partial.join(", ") }));
  const bar = f.total ? `<div class="fill-bar" role="progressbar" aria-label="${esc(t(f.method === "api" ? "Months done" : "Import progress"))}" aria-valuemin="0" aria-valuemax="${f.total}" aria-valuenow="${f.done}"><i style="width:${Math.round((f.progress || 0) * 100)}%"></i></div>` : "";
  const error = ["stuck", "failed"].includes(f.status) && f.last_error ? `<div class="sub error-text">${esc(f.last_error)}</div>` : "";
  return `<li class="fill">
    <div class="fill-head"><b>${esc(what)}</b> <span class="chip ${tone}">${esc(t(word))}</span></div>
    ${bar}
    <div class="sub">${esc([f.message ? t(f.message) : null, ...facts].filter(Boolean).join(" · "))}</div>
    ${error}${canWrite ? actions(f) : ""}
  </li>`;
}

function actions(f) {
  const button = (action, label) => `<button class="btn" type="button" data-fill="${f.id}" data-action="${action}">${esc(label)}</button>`;
  const list = [];
  if (f.method === "api") {
    if (f.status === "running" || f.status === "waiting") list.push(button("pause", t("Pause")));
    if (f.status === "paused") list.push(button("resume", t("Resume")));
    if (f.status === "stuck") list.push(button("resume", t("Try again")));
    if (!ACTIVE.includes(f.status) && f.partial?.length) list.push(button("retry", t("Fetch partial months again")));
  }
  if ([...ACTIVE, "stuck"].includes(f.status)) list.push(button("cancel", t("Cancel")));
  return list.length ? `<div class="fill-actions">${list.join("")}</div>` : "";
}

function month(iso) {
  return iso ? monthYear(iso) : "…";
}

function when(iso) {
  const day = String(iso).slice(0, 10);
  const wd = weekday(day);
  return `${wd} ${userDateTime(iso)}`;
}

export function mb(bytes) {
  return `${number(bytes / 1024 / 1024)} MB`;
}
