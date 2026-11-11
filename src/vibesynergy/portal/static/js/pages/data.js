// Sync (#/data): is everything arriving? Sources, problems (with a retry), a coverage map of
// days with data per month, the job queue, a form to re-sync any period, and every dataset's
// sync state and archive size. Filling history and imports are on History (#/history).
import { get, post, upload } from "../api.js";
import {
  addDays,
  ago,
  esc,
  metricLabel,
  monthName,
  number,
  parseDay,
  shortDate,
  sourceName,
} from "../format.js";
import { N, t } from "../i18n.js";
import { bindPeriodPicker, periodPickerHtml, resolvePeriod } from "../period_picker.js";
import { syncAdvice, syncDetails } from "../sync_error.js";
import { mb, uploadResumable } from "./history.js";

const RESYNC_DEFAULT = (today) => ({ start: addDays(today, -6), end: today });

const COVERAGE_SPANS = [
  ["12", N("12 months")],
  ["24", N("2 years")],
  ["60", N("5 years")],
  ["240", N("Everything")],
];
const sourceTitle = (source) => source.id === "caldav"
  ? sourceName(source.id)
  : (source.name || sourceName(source.id));

export async function render(view, { query, meta, stale }, { embedded = false } = {}) {
  const months = query.get("months") || "24";
  const [reply, coverage] = await Promise.all([get("data"), get("coverage", { months })]);
  if (stale()) return;
  if (!embedded) document.title = `${t("Sync")} · VibeSynergy`;
  const hashBase = embedded ? "#/settings/data/sync" : "#/data";
  const { data } = reply;
  const today = meta.data.today;
  const freshness = reply.meta.freshness;
  const catalogue = meta.data.metrics;
  const counts = data.jobs.counts;
  const share = meta.data.share;
  const canWrite = !share || share.can_write; // a read-only shared view has no sync buttons
  const canSync = canWrite && data.sources.some((s) => s.enabled && s.configured !== false && s.datasets.length);

  const titleTag = embedded ? "h2" : "h1";
  view.innerHTML = `
    <div class="page-head">
      <div><${titleTag}>${t("Sync")}</${titleTag}>
        <div class="sub">${esc(t("{n} datasets", { n: number(data.datasets.length) }))} · ${esc(data.problems.length ? t("{n} with a problem", { n: data.problems.length }) : t("no problems"))} · ${esc(t("jobs: {counts}", { counts: jobCounts(counts) }))}</div></div>
      <button class="btn" type="button" data-action="reload">${t("Refresh")}</button>
    </div>
    <div class="flash" role="status"></div>
    <p class="sub" data-latency>${latencyLine(data.latency?.[0])}</p>
    <p class="sub">${t("A finished sync doesn't guarantee every day has data; check the coverage below.")}</p>
    <section class="grid tiles">${data.sources.map((s) => sourceCard(s, data, freshness)).join("")}</section>
    ${data.problems.length ? `<section class="card">${problemsHtml(data.problems)}</section>` : ""}
    <section class="card">${coverageHtml(coverage, catalogue, months)}</section>
    <section class="grid two">
      ${canSync ? `<div class="card">${resyncHtml(data.sources, today)}</div>` : ""}
      <div class="card">${jobsHtml(data.jobs.recent)}</div>
    </section>
    <section class="card">${datasetsHtml(data)}</section>`;
  if (!canWrite) view.querySelectorAll("[data-retry]").forEach((b) => b.remove());

  const flash = view.querySelector(".flash");
  const say = (text, error = false) => {
    flash.innerHTML = `<div class="notice${error ? " error" : ""}">${esc(text)}</div>`;
  };

  view.querySelector('[data-action="reload"]').addEventListener("click", () => {
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
  view.querySelector("select[name=months]").addEventListener("change", (event) => {
    location.hash = `${hashBase}?months=${event.target.value}`;
  });

  const resync = async (body, button) => {
    button.disabled = true;
    try {
      const result = await post("resync", body);
      const queued = result.data.queued;
      const fresh = queued.filter((q) => q.job !== null).length;
      const what = t("{source}{dataset}, {start} to {end}", { source: sourceName(body.source), dataset: body.dataset ? ` ${body.dataset}` : "", start: body.start, end: body.end });
      say(
        fresh === 0
          ? t("Already queued: {what}.", { what })
          : result.data.background
            ? t("Queued {n} monthly jobs for {what}. They run in the background, oldest first, after the regular syncs; progress shows under Jobs.", { n: fresh, what })
            : t("Queued a sync of {what}. It runs within a minute; press Refresh to follow it.", { what }),
      );
    } catch (error) {
      say(t("Could not queue the sync: {error}", { error: error.message }), true);
    } finally {
      button.disabled = false;
    }
  };

  for (const button of view.querySelectorAll("[data-retry]")) {
    button.addEventListener("click", () => {
      const [source, dataset] = button.dataset.retry.split("/");
      resync({ source, dataset, start: addDays(today, -6), end: today }, button);
    });
  }

  const form = view.querySelector("form.resync");
  if (!form) return;
  const sourceSelect = form.querySelector("[name=source]");
  const datasetSelect = form.querySelector("[name=dataset]");
  const fillDatasets = () => {
    const source = data.sources.find((s) => s.id === sourceSelect.value);
    datasetSelect.innerHTML = `<option value="">${t("All datasets")}</option>${(source?.datasets || [])
      .map((d) => `<option value="${esc(d.name)}">${esc(d.name)}</option>`)
      .join("")}`;
  };
  sourceSelect.addEventListener("change", fillDatasets);
  fillDatasets();
  const resyncPicker = bindPeriodPicker(view, {
    id: "resync",
    value: RESYNC_DEFAULT(today),
    today,
    max: today,
    applyButton: false,
    onChange: () => {},
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const f = new FormData(form);
    const message = resyncPicker.validate();
    if (message) return;
    const { start, end } = resolvePeriod(resyncPicker.value(), today);
    resync(
      { source: f.get("source"), dataset: f.get("dataset") || null, start, end },
      form.querySelector("button"),
    );
  });
}

const ON_THE_WAY_MS = 2 * 60 * 60 * 1000;

function latencyLine(item, now = Date.now()) {
  if (!item) return t("Finish a workout as usual. Its delivery time will appear here.");
  const seconds = item.delays_s?.total_s;
  if (seconds == null) {
    const mark = item.finished_at || item.received_at || item.source_event_at;
    const at = mark ? Date.parse(mark) : NaN;
    if (!Number.isFinite(at) || at > now || now - at > ON_THE_WAY_MS) return "";
    return t("Your latest workout is still on its way. Check again shortly.");
  }
  const minutes = Math.max(1, Math.round(seconds / 60));
  return esc(t("Your latest workout reached the dashboard in {minutes} minutes.", { minutes: number(minutes) }));
}

function jobCounts(counts) {
  const order = ["queued", "running", "done", "dead"];
  const names = { queued: N("waiting"), running: N("running"), done: N("done"), dead: N("failed") };
  return order.filter((k) => counts[k]).map((k) => `${number(counts[k])} ${t(names[k])}`).join(", ") || t("none");
}

function sourceCard(source, data, freshness) {
  const datasets = data.datasets.filter((d) => d.source === source.id);
  const problems = data.problems.filter((d) => d.source === source.id);
  const pending = datasets.reduce((n, d) => n + (d.processing?.pending || 0), 0);
  const chip = problems.length
    ? `<span class="chip warning">${esc(problems.length === 1 ? t("1 problem") : t("{n} problems", { n: problems.length }))}</span>`
    : pending
      ? `<span class="chip warning">${esc(t("Awaiting processing: {n}", { n: number(pending) }))}</span>`
      : !source.enabled
        ? `<span class="chip info">${t("not enabled")}</span>`
        : source.configured === false
          ? `<span class="chip warning">${t("not set up")}</span>`
          : `<span class="chip success">${t("no recorded errors")}</span>`;
  return `<div class="card tile">
    <div class="label">${esc(sourceTitle(source))}</div>
    <div class="value" style="font-size:20px">${esc(freshness[source.id] ? ago(freshness[source.id]) : t(datasets.some((d) => d.processing?.current_items) ? "local archive" : "never synced"))}</div>
    <div class="context">${chip} <span class="muted">${esc(t("{n} datasets", { n: datasets.length }))}</span></div>
  </div>`;
}

function problemsHtml(problems) {
  const rows = problems
    .map((p) => {
      const mapping = p.processing?.errors
        ? `<div>${esc(t("Current archive items with processing errors: {n}.", { n: number(p.processing.errors) }))}</div><div class="sub">${t("Check the source files or importer; downloading unchanged data again does not repair processing errors.")}</div>`
        : "";
      return `<tr>
        <th scope="row">${esc(sourceName(p.source))} <b>${esc(p.dataset)}</b></th>
        <td>${p.last_error ? `<div>${esc(syncAdvice(p.source, p.last_error))}</div>${syncDetails(p.last_error)}` : ""}${mapping}</td>
        <td class="muted">${esc(p.last_success_at ? ago(p.last_success_at) : t("no download recorded"))}</td>
        <td>${p.last_error ? `<button class="btn" type="button" data-retry="${esc(p.source)}/${esc(p.dataset)}">${t("Retry last 7 days")}</button>` : ""}</td>
      </tr>`;
    })
    .join("");
  return `<h2>${t("Problems")}</h2><div class="table-wrap"><table>
    <thead><tr><th scope="col">${t("Dataset")}</th><th scope="col">${t("Last error")}</th><th scope="col">${t("Last successful sync/import")}</th><th scope="col">${t("Action")}</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

// Coverage: days with data per month. A sequential ramp (one hue, light to dark); the
// numbers are in each cell's title and the table stays readable without colour.
const BINS = [
  [0, N("none"), "cov-0"],
  [0.25, N("under 25%"), "cov-1"],
  [0.6, "25-60%", "cov-2"],
  [0.95, "60-95%", "cov-3"],
  [1.01, N("95% or more"), "cov-4"],
];

function binFor(share) {
  if (share === 0) return BINS[0];
  return BINS.slice(1).find(([limit]) => share < limit) || BINS[BINS.length - 1];
}

function coverageHtml(coverage, catalogue, months) {
  const { months: list, days_with_data: matrix } = coverage.data;
  const daysIn = (ym) => coverage.data.days_in_window[ym];
  const years = [];
  for (const ym of list) {
    const year = ym.slice(0, 4);
    if (years.length && years[years.length - 1][0] === year) years[years.length - 1][1]++;
    else years.push([year, 1]);
  }
  const yearRow = years.map(([year, n]) => `<th class="cov-year" scope="colgroup" colspan="${n}" aria-label="${esc(year)}">${n >= 3 ? esc(year) : `<span class="visually-hidden">${esc(year)}</span>`}</th>`).join("");
  const head = list
    .map((ym) => `<th class="cov-month" scope="col" aria-label="${esc(ym)}" title="${esc(ym)}">${esc(monthName(`${ym}-01`, "narrow"))}</th>`)
    .join("");
  const rows = Object.entries(matrix)
    .map(([metric, byMonth]) => {
      const label = metric === "activity.count" ? t("Activities") : metricLabel(metric, catalogue);
      const cells = list
        .map((ym) => {
          const days = byMonth[ym] || 0;
          const [, , cls] = binFor(days / daysIn(ym));
          const description = esc(t("{metric}, {month}: {n} of {total} days", { metric: label, month: ym, n: days, total: daysIn(ym) }));
          return `<td class="cov ${cls}" aria-label="${description}" title="${description}"></td>`;
        })
        .join("");
      return `<tr><th class="cov-label" scope="row">${esc(label)}</th>${cells}</tr>`;
    })
    .join("");
  const legend = BINS.map(([, text, cls]) => `<span><i class="cov ${cls}"></i>${esc(t(text))}</span>`).join("");
  return `<div class="page-head" style="margin-bottom:6px"><h2 style="margin:0">${t("Coverage")}</h2>
      <label class="sub">${t("Show")} <select class="btn" name="months">${COVERAGE_SPANS.map(([v, text]) => `<option value="${v}"${v === months ? " selected" : ""}>${esc(t(text))}</option>`).join("")}</select></label></div>
    <p class="sub" style="margin-top:0">${esc(t("Days with data from {date} in the selected period. A day without data may still have had activity.", { date: shortDate(coverage.meta.period.start) }))}</p>
    <div class="table-wrap" role="region" aria-label="${esc(t("Coverage"))}" tabindex="0"><table class="coverage"><thead><tr><td></td>${yearRow}</tr><tr><th scope="col">${t("Dataset")}</th>${head}</tr></thead><tbody>${rows}</tbody></table></div>
    <div class="legend" style="margin-top:8px">${legend}</div>`;
}

function resyncHtml(sources, today) {
  const enabled = sources.filter((s) => s.enabled && s.configured !== false && s.datasets.length);
  const value = RESYNC_DEFAULT(today);
  return `<h2>${t("Re-sync a period")}</h2>
    <p class="sub" style="margin-top:0">${t("Fetch a period again, for example when a day is missing or wrong. Up to a month runs right away; longer periods run month by month in the background, like a backfill.")}</p>
    <form class="resync filters">
      <label>${t("Source")} <select class="btn" name="source">${enabled.map((s) => `<option value="${esc(s.id)}">${esc(sourceTitle(s))}</option>`).join("")}</select></label>
      <label>${t("Dataset")} <select class="btn" name="dataset"></select></label>
      ${periodPickerHtml({ id: "resync", value, today, max: today, applyButton: false })}
      <button class="btn primary" type="submit">${t("Queue sync")}</button>
    </form>`;
}

function jobText(job) {
  const p = job.payload || {};
  const what = [p.source && sourceName(p.source), p.dataset, p.key, p.window].filter(Boolean).join(" · ");
  const kinds = { sync: N("Sync"), fetch_one: N("Fetch"), tombstone: N("Delete"), normalise: N("Re-process") };
  return `${kinds[job.kind] ? t(kinds[job.kind]) : job.kind}${what ? `: ${what}` : ""}`;
}

function jobsHtml(jobs) {
  if (!jobs.length) return `<h2>${t("Jobs")}</h2><div class="empty">${t("No jobs yet.")}</div>`;
  const status = {
    queued: ["", t("waiting")],
    running: ["good", t("running")],
    done: ["", t("done")],
    dead: ["critical", t("failed")],
  };
  const rows = jobs
    .map((j) => {
      const [tone, word] = status[j.status] || ["", j.status];
      const when = j.finished_at || j.started_at || j.created_at;
      const retry = j.status === "queued" && j.attempts ? ` <span class="muted">(${esc(t("attempt {n}", { n: j.attempts + 1 }))})</span>` : "";
      return `<tr>
        <th scope="row">${esc(jobText(j))}${j.last_error && j.status !== "done" ? `<div class="sub">${esc(syncAdvice(j.payload?.source, j.last_error))}</div>${syncDetails(j.last_error)}` : ""}</th>
        <td><span class="chip ${tone}">${esc(word)}</span>${retry}</td>
        <td class="muted">${esc(ago(when))}</td>
      </tr>`;
    })
    .join("");
  return `<h2>${t("Jobs")}</h2><div class="table-wrap jobs"><table>
    <thead><tr><th>${t("Job")}</th><th>${t("Status")}</th><th>${t("When")}</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function size(bytes) {
  if (!bytes) return "–";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = bytes;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${number(v, v < 10 && i ? 1 : 0)} ${units[i]}`;
}

function processingHtml(p) {
  if (!p || !p.current_items) return esc(t("No archived items"));
  const states = [];
  if (p.errors) states.push(`<span class="chip warning">${esc(t("Processing errors: {n}", { n: number(p.errors) }))}</span>`);
  if (p.pending) states.push(esc(t("Awaiting processing: {n}", { n: number(p.pending) })));
  if (!p.errors && !p.pending) states.push(esc(t("Processed; check observed coverage")));
  return states.join(" · ");
}

function datasetsHtml(data) {
  const archive = Object.fromEntries(data.archive.map((a) => [`${a.source}/${a.dataset}`, a]));
  const groups = data.sources
    .map((source) => {
      const rows = data.datasets
        .filter((d) => d.source === source.id)
        .map((d) => {
          const a = archive[`${d.source}/${d.dataset}`] || {};
          const range = d.synced_until ? t("{from} to {until}", { from: d.synced_from ?? "…", until: d.synced_until }) : t("current state");
          return `<tr>
            <th scope="row">${esc(d.dataset)}${d.problem_stages?.length ? ` <span class="chip warning">${t("problem")}</span>` : ""}</th>
            <td class="muted">${esc(range)}</td>
            <td class="muted">${esc(d.last_success_at ? ago(d.last_success_at) : t("never"))}</td>
            <td class="num">${number(a.items)}</td>
            <td class="num">${esc(size(a.bytes))}</td>
            <td>${processingHtml(d.processing)}</td>
          </tr>`;
        })
        .join("");
      if (!rows) return "";
      return `<details${source.enabled ? "" : ""}><summary>${esc(sourceTitle(source))}</summary>
        <div class="table-wrap"><table>
        <thead><tr><th>${t("Dataset")}</th><th>${t("Synced")}</th><th>${t("Last successful sync/import")}</th><th class="num">${t("Items")}</th><th class="num">${t("Archive")}</th><th>${t("Archive processing")}</th></tr></thead>
        <tbody>${rows}</tbody></table></div></details>`;
    })
    .join("");
  return `<h2>${t("Datasets")}</h2><p class="sub" style="margin-top:0">${t("Every dataset per source: the dates synced so far, the last successful sync, and what is kept in the raw archive (items, size, and records that could not be read).")}</p>${groups}`;
}

// Import files: upload to the drop folder (imported at once) and what came in lately.
const BIG_UPLOAD = 100 * 1024 * 1024;
export async function filesHtml(el, stale) {
  const reply = await get("files");
  if (stale()) return;
  const d = reply.data;
  const rows = d.history
    .slice(0, 12)
    .map(
      (h) => `<tr><td>${esc(h.file)}</td><td>${esc(ago(h.at))}</td>
        <td>${h.status === "failed" ? `<span class="chip warning">${t("failed")}</span> ${esc(h.error || "")}` : `<span class="chip ${h.imported ? "good" : ""}">${esc(t(h.status))}</span>`}</td>
        <td class="num">${h.imported ?? ""}${h.skipped ? ` <span class="muted">(${esc(t("{n} already here", { n: h.skipped }))})</span>` : ""}</td></tr>`,
    )
    .join("");
  el.innerHTML = `<h2>${t("Import files")}</h2>
    <p class="sub">${esc(t("FIT, GPX or TCX files, or a zip of them (e.g. from MyWhoosh, Zwift, Komoot or an old device). They're merged with the copies VibeSynergy already has. You can also put files in the drop folder {folder} on this server; it's checked every {n} minutes.", { folder: d.folder, n: d.every_minutes }))}</p>
    <label class="drop-zone"><input type="file" multiple accept=".fit,.gpx,.tcx,.zip,.FIT,.GPX,.TCX,.ZIP" hidden>
      <span>${t("Drop files here or")} <u>${t("choose files")}</u></span></label>
    <div class="sub" data-upload-status></div>
    ${rows ? `<div class="table-wrap"><table><thead><tr><th>${t("File")}</th><th>${t("When")}</th><th>${t("Result")}</th><th class="num">${t("Activities")}</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}`;
  const input = el.querySelector("input[type=file]");
  const zone = el.querySelector(".drop-zone");
  const status = el.querySelector("[data-upload-status]");
  const send = async (all) => {
    if (!all.length) return;
    status.classList.remove("error-text");
    // A large zip (an account export of several GB) goes up in chunks: it can continue
    // after a dropped connection, and the archive is then imported in the background.
    const big = all.filter((f) => /\.zip$/i.test(f.name) && f.size > BIG_UPLOAD);
    const files = all.filter((f) => !big.includes(f));
    try {
      for (const file of big) {
        await uploadResumable(file, (received, total, note) => {
          status.textContent = note || t("Uploading {file}: {pct}% ({done} of {total})", { file: file.name, pct: Math.round((received / total) * 100), done: mb(received), total: mb(total) });
        });
        status.textContent = t("{file} is uploaded. It's being imported now; you can follow it under History.", { file: file.name });
      }
      if (!files.length) return;
      status.textContent = files.length === 1 ? t("Importing 1 file…") : t("Importing {n} files…", { n: files.length });
      const result = await upload("files/upload", files);
      const r = result.data.results;
      const added = r.reduce((n, x) => n + (x.imported || 0), 0);
      const failed = r.filter((x) => x.status === "failed");
      status.textContent = (added === 1 ? t("1 activity imported") : t("{n} activities imported", { n: added })) + (failed.length ? `, ${t("{n} failed", { n: failed.length })}` : "") + ".";
      if (!stale()) filesHtml(el, stale);
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("error-text");
    }
  };
  input.addEventListener("change", () => send([...input.files]));
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("over");
    send([...event.dataTransfer.files]);
  });
}
