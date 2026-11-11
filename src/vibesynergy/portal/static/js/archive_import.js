// Sources -> Garmin / Strava: bring the history in from the source's account export.
// The APIs are only asked for recent data, so the past comes from this zip. It uploads in
// resumable chunks (the real files are several GB), then the server imports it in the
// background and this control shows the progress until it's done. Importing twice is
// harmless: what is already here is recognised and left alone.
import { get, post } from "./api.js";
import { esc, number } from "./format.js";
import { N, t } from "./i18n.js";
import { mb, uploadResumable } from "./pages/history.js";

const POLL_MS = 3000;

// Per source: which archive it fills, and its texts. Strava counts files; Garmin's export
// hides most files in nested zips, so its progress is a share of the zip.
const KINDS = {
  strava: {
    archive: "strava_export",
    title: N("Strava history"),
    intro: N("Your older activities come from your Strava export. How to request it:"),
    link: N("Strava: export your account"),
    choose: N("Choose the Strava zip"),
    wrongFile: N("Choose the Strava .zip file."),
    counted: true,
  },
  garmin: {
    archive: "garmin_export",
    title: N("Garmin history"),
    intro: N("Your older activities, sleep and daily data come from your Garmin export. How to request it:"),
    link: N("Garmin: export your data"),
    choose: N("Choose the Garmin zip"),
    wrongFile: N("Choose the Garmin .zip file."),
    counted: false,
  },
};

export async function archiveImport(el, { stale, source }) {
  const kind = KINDS[source];
  if (!kind) return;
  let reply;
  try {
    reply = await get("history");
  } catch (error) {
    el.innerHTML = `<div class="notice error" role="alert">${esc(error.message)}</div>`;
    return;
  }
  if (stale() || !el.isConnected) return;
  const how = reply.data.exports?.[source];
  const ARCHIVE = kind.archive;
  el.innerHTML = `<h3>${t(kind.title)}</h3>
    <p class="sub">${t(kind.intro)}
      ${how ? `<a href="${esc(how.url)}" target="_blank" rel="noopener">${t(kind.link)}</a>` : ""}</p>
    ${how?.steps ? `<p class="sub">${esc(t(how.steps))}</p>` : ""}
    <p class="sub">${t("Upload the zip as you received it; don't unpack it. If the upload stops, choose the same file again and it continues.")}</p>
    <div class="archive-import-actions">
      <button class="btn primary" type="button" data-choose>${t(kind.choose)}</button>
      <button class="btn" type="button" data-stop hidden>${t("Stop importing")}</button>
      <input type="file" accept=".zip,.ZIP" hidden aria-label="${esc(t(kind.choose))}">
    </div>
    <div class="fill-bar" role="progressbar" aria-label="${esc(t("Import progress"))}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" hidden><i></i></div>
    <p class="sub" role="status" aria-live="polite" data-import-status></p>
    <details data-import-details hidden><summary>${t("Details")}</summary><p class="sub" data-import-detail></p></details>`;
  const choose = el.querySelector("[data-choose]");
  const stop = el.querySelector("[data-stop]");
  const input = el.querySelector("input[type=file]");
  const bar = el.querySelector(".fill-bar");
  const status = el.querySelector("[data-import-status]");
  const details = el.querySelector("[data-import-details]");
  let timer = null;
  let current = null;
  let uploading = false;

  const say = (text, percent = null) => {
    status.textContent = text;
    bar.hidden = percent === null;
    if (percent !== null) {
      bar.firstElementChild.style.width = `${percent}%`;
      bar.setAttribute("aria-valuenow", String(percent));
    }
  };

  const show = (fill) => {
    current = fill;
    const active = fill && ["running", "waiting", "paused"].includes(fill.status);
    stop.hidden = !active;
    choose.disabled = Boolean(active) || uploading;
    details.hidden = !(fill?.last_error || fill?.message);
    el.querySelector("[data-import-detail]").textContent = [fill?.message ? t(fill.message) : null, fill?.last_error].filter(Boolean).join(" · ");
    if (!fill) return say("");
    if (active) {
      if (!fill.total) return say(t("Importing: getting started…"), null);
      const percent = Math.round((fill.done / fill.total) * 100);
      if (kind.counted) return say(t("Importing: {done} of {total}", { done: number(fill.done), total: number(fill.total) }), percent);
      return say(t("Importing: {pct}%", { pct: percent }), percent);
    }
    if (fill.status === "done") {
      if (kind.counted) return say(t("Done: {n} files checked. Your activities are ready.", { n: number(fill.total || fill.done) }), null);
      return say(t("Done: your Garmin history is in. Anything you already had was left alone."), null);
    }
    if (fill.status === "cancelled") return say(t("Importing was stopped. Choose the same file again to continue."), null);
    say(t(fill.message || "Import failed. Check the file and try again."), null);
  };

  const refresh = async () => {
    clearTimeout(timer);
    let fills;
    try {
      fills = (await get("history")).data.fills;
    } catch (error) {
      say(error.message);
      return;
    }
    if (stale() || !el.isConnected) return;
    if (!uploading) show(fills.find((f) => f.source === ARCHIVE) || null);
    const active = fills.some((f) => f.source === ARCHIVE && f.status === "running");
    if (active || uploading) timer = setTimeout(refresh, POLL_MS);
  };

  const send = async (file) => {
    if (!file) return;
    if (!/\.zip$/i.test(file.name)) return say(t(kind.wrongFile));
    uploading = true;
    choose.disabled = true;
    try {
      await uploadResumable(file, (received, total, note) => {
        const percent = Math.round((received / total) * 100);
        say(note || t("Uploading: {done} of {total}", { done: mb(received), total: mb(total) }), percent);
      }, { expect: ARCHIVE });
    } catch (error) {
      uploading = false;
      choose.disabled = false;
      input.value = "";
      say(error.message);
      return;
    }
    uploading = false;
    input.value = "";
    await refresh();
  };

  choose.addEventListener("click", () => input.click());
  input.addEventListener("change", () => send(input.files[0]));
  stop.addEventListener("click", async () => {
    if (!current) return;
    stop.disabled = true;
    try {
      await post(`history/${current.id}/cancel`);
    } catch (error) {
      say(error.message);
    }
    stop.disabled = false;
    await refresh();
  });
  await refresh();
}
