// The workout's photos and videos on its page (#114): the ones you add, and the ones on its
// Strava and Garmin copies, which come in by themselves. A video plays in the browser's own
// player, from a copy without the file's metadata; the poster is one of its frames. Strava and Garmin take no photos from other
// apps, so a photo added here stays in VibeSynergy. Removing one only removes it here; it
// stays on Strava and Garmin and can be put back from the history.
//
// A shared page shows the photos only (no buttons, no history, never a removed photo).
import { get, post, upload, url } from "./api.js";
import { esc, userDateTime } from "./format.js";
import { N, t } from "./i18n.js";

const SOURCE = { vs: "VibeSynergy", strava: "Strava", garmin: "Garmin" };
const ACTION = { added: N("Added"), merged: N("Also came in"), removed: N("Removed"), restored: N("Put back") };
const ACCEPT = "image/jpeg,image/png,image/webp,image/gif,video/mp4,video/quicktime,.mp4,.mov";
const VIDEO_FILE = /\.(mp4|mov|m4v)$/i;

function photoUrl(id, size) {
  return url(`activity-media/photo/${encodeURIComponent(id)}`, { size });
}

// `v` names the playable copy, so a copy made again is never taken from the browser's cache.
function videoUrl(p) {
  return url(`activity-media/video/${encodeURIComponent(p.id)}`, { v: p.stream || "" });
}

function altText(p, n, total) {
  if (p.caption) return p.caption;
  return p.kind === "video" ? t("Video {n} of {total}", { n, total }) : t("Photo {n} of {total}", { n, total });
}

// 75 -> "1:15"
export function clock(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function itemHtml(p, alt) {
  if (p.kind === "video") {
    return `<video controls preload="none" playsinline poster="${esc(photoUrl(p.id, "thumb"))}" aria-label="${esc(alt)}"><source src="${esc(videoUrl(p))}" type="video/mp4"></video>`;
  }
  return `<a href="${esc(photoUrl(p.id, "display"))}" target="_blank" rel="noopener" aria-label="${esc(t("{photo}, full size", { photo: alt }))}"><img src="${esc(photoUrl(p.id, "thumb"))}" alt="${esc(alt)}" loading="lazy" decoding="async"></a>`;
}

// "Strava", "Garmin" or both, in words.
function places(list) {
  const named = list.filter((s) => s === "strava" || s === "garmin");
  if (named.length > 1) return t("Strava and Garmin");
  return named.length ? SOURCE[named[0]] : "";
}

function placeText(p) {
  const from = places(p.places);
  return from ? t("From {places}", { places: from }) : "";
}

function gridHtml(photos, shared) {
  const total = photos.length;
  const items = photos.map((p, i) => {
    const alt = altText(p, i + 1, total);
    const video = p.kind === "video";
    const from = [video && p.duration_s ? clock(p.duration_s) : "", placeText(p)].filter(Boolean).join(" · ");
    const label = video ? t("Remove video {n}", { n: i + 1 }) : t("Remove photo {n}", { n: i + 1 });
    const remove = shared ? "" : `<button class="btn wm-remove" type="button" data-wm-remove="${esc(p.id)}" aria-label="${esc(label)}">${esc(t("Remove"))}</button>`;
    return `<li>${itemHtml(p, alt)}
      ${from || remove ? `<div class="wm-meta">${from ? `<span class="sub">${esc(from)}</span>` : ""}${remove}</div>` : ""}</li>`;
  }).join("");
  return `<ul class="wm-grid" role="list">${items}</ul>`;
}

function whereText(d) {
  const from = places(d.from || []);
  const kept = t("Photos and videos you add stay in VibeSynergy: Strava and Garmin take none from other apps. Removing one only removes it here.");
  if (!from) return kept;
  return `${t("Photos and videos on {places} come in by themselves.", { places: from })} ${kept}`;
}

function historyHtml(d, tz) {
  const removed = d.removed || [];
  const rows = d.history || [];
  if (!rows.length && !removed.length) return "";
  const back = removed.map((p) => `<li class="wm-removed"><img src="${esc(photoUrl(p.id, "thumb"))}" alt="${esc(p.caption || (p.kind === "video" ? t("Removed video") : t("Removed photo")))}" loading="lazy">
      <button class="btn" type="button" data-wm-restore="${esc(p.id)}">${esc(t("Put back"))}</button></li>`).join("");
  const changes = rows.map((h) => {
    const what = h.action === "merged"
      ? t("Also came in from {source}", { source: SOURCE[h.source] || h.source })
      : `${SOURCE[h.source] || h.source} · ${t(ACTION[h.action] || h.action)}`;
    const which = h.number ? ` · ${h.kind === "video" ? t("video {n}", { n: h.number }) : t("photo {n}", { n: h.number })}` : "";
    return `<li><div class="sub">${esc(what)}${esc(which)} · <time datetime="${esc(h.at)}">${esc(userDateTime(h.at, tz))}</time></div></li>`;
  }).join("");
  return `<details class="wt-history" data-wm-history><summary>${esc(t("History ({n})", { n: rows.length }))}</summary>
    ${back ? `<h3>${esc(t("Removed photos and videos"))}</h3><ul class="wm-grid" role="list">${back}</ul>` : ""}
    <ol class="wt-versions">${changes}</ol></details>`;
}

export function mediaHtml(d, { shared, tz }) {
  const photos = d.photos || [];
  const grid = photos.length ? gridHtml(photos, shared) : shared ? "" : `<p class="sub">${esc(t("No photos or videos yet."))}</p>`;
  if (shared) return `<h2 id="wm-title">${t("Photos and videos")}</h2>${grid}`;
  return `<h2 id="wm-title">${t("Photos and videos")}</h2>
    <div data-wm-photos tabindex="-1">${grid}</div>
    <div class="ql-actions"><button class="btn" type="button" data-wm-add aria-describedby="wm-where">${esc(t("Add photos or videos"))}</button>
      <input type="file" accept="${ACCEPT}" multiple hidden data-wm-file></div>
    <p class="sub" id="wm-where">${esc(whereText(d))}</p>
    <p class="sub" role="status" data-wm-status></p>
    ${historyHtml(d, tz)}`;
}

// What an upload did, in one or two short sentences.
export function resultText(results, maxMb, maxVideoMb) {
  const added = (r) => r.state === "added" || r.restored;
  const photos = results.filter((r) => added(r) && r.kind !== "video").length;
  const videos = results.filter((r) => added(r) && r.kind === "video").length;
  const same = results.filter((r) => r.state === "same" && !r.restored).length;
  const parts = [];
  if (photos) parts.push(t("Photos added: {n}.", { n: photos }));
  if (videos) parts.push(t("Videos added: {n}.", { n: videos }));
  if (same) parts.push(t("Already here: {n}.", { n: same }));
  for (const r of results.filter((x) => x.state === "refused")) {
    const mb = VIDEO_FILE.test(r.file) ? maxVideoMb : maxMb;
    if (r.code === "too_large") parts.push(t("{file} is larger than {mb} MB.", { file: r.file, mb }));
    else if (r.code === "too_many") parts.push(t("This workout has as many photos and videos as it can hold."));
    else if (r.code === "no_video_tool") parts.push(t("{file} is a video, and videos can't be added here yet. Photos still can.", { file: r.file }));
    else parts.push(t("{file} can't be shown. Choose a JPEG, PNG, WebP or GIF photo, or an MP4 or MOV video.", { file: r.file }));
  }
  return parts.join(" ");
}

// Mount the card into `el`. A shared page with no photos shows nothing.
export async function workoutMedia(el, id, { stale, tz, shared = false }) {
  if (!el) return;
  let data;
  try {
    data = (await get("activity-media", { id })).data;
  } catch (error) {
    if (!shared && !stale() && el.isConnected) {
      el.innerHTML = `<p class="sub error-text">${esc(error.message)}</p>`;
      el.hidden = false;
    }
    return;
  }
  if (stale() || !el.isConnected) return;
  if (shared && !(data.photos || []).length) return;

  const draw = (message, focus, error = false) => {
    const open = el.querySelector("[data-wm-history]")?.open;
    el.innerHTML = mediaHtml(data, { shared, tz });
    el.hidden = false;
    if (shared) return;
    if (open) el.querySelector("[data-wm-history]").open = true;
    bind();
    const status = el.querySelector("[data-wm-status]");
    if (message) status.textContent = message;
    status.classList.toggle("error-text", error);
    if (focus) (el.querySelector(focus) || el.querySelector("[data-wm-photos]"))?.focus();
  };

  const busy = () => {
    const status = el.querySelector("[data-wm-status]");
    status.classList.remove("error-text");
    status.textContent = t("Saving…");
  };

  const change = async (path, media) => {
    busy();
    try {
      const reply = await post(path, { id, media });
      if (stale() || !el.isConnected) return;
      data = { ...data, ...reply.data };
      draw(path.endsWith("restore") ? t("Put back.") : t("Removed from VibeSynergy. Nothing is deleted on Strava or Garmin."), "[data-wm-photos]");
    } catch (error) {
      el.querySelector("[data-wm-status]").classList.add("error-text");
      el.querySelector("[data-wm-status]").textContent = t("Not saved: {error}", { error: error.message });
    }
  };

  const bind = () => {
    const input = el.querySelector("[data-wm-file]");
    el.querySelector("[data-wm-add]")?.addEventListener("click", () => input.click());
    input?.addEventListener("change", async () => {
      const files = [...input.files];
      if (!files.length) return;
      busy();
      try {
        const reply = await upload("activity-media/upload", files, { id });
        if (stale() || !el.isConnected) return;
        const { results, ...rest } = reply.data;
        data = { ...data, ...rest };
        const refused = results.some((r) => r.state === "refused");
        draw(resultText(results, data.max_mb, data.max_video_mb), "[data-wm-add]", refused && results.every((r) => r.state === "refused"));
      } catch (error) {
        const status = el.querySelector("[data-wm-status]");
        status.classList.add("error-text");
        status.textContent = t("Not saved: {error}", { error: error.message });
      }
    });
    for (const button of el.querySelectorAll("[data-wm-remove]")) {
      button.addEventListener("click", () => change("activity-media/remove", button.dataset.wmRemove));
    }
    for (const button of el.querySelectorAll("[data-wm-restore]")) {
      button.addEventListener("click", () => change("activity-media/restore", button.dataset.wmRestore));
    }
  };

  draw();
}
