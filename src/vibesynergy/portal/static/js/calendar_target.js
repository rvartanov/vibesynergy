// Shared Garmin target selector for Settings → Calendar and Data → Sources.
import { get, post } from "./api.js";
import { esc, userDate } from "./format.js";
import { t } from "./i18n.js";

export async function garminTargetSettings(el, { stale, status, sources = false } = {}) {
  const data = status || (await get("caldav")).data;
  if (stale?.() || !el.isConnected) return;
  const calendars = data.target_calendars || [];
  el.innerHTML = `<div class="field">
    <label for="garmin-target-calendar">${t("Calendar for Garmin workouts")}</label>
    <p class="sub" id="garmin-target-help">${t("Choose where Garmin workouts will appear.")}</p>
    ${calendars.length ? `<select class="btn" id="garmin-target-calendar" aria-describedby="garmin-target-help">
      <option value="">${t("None")}</option>
      ${calendars.map((c) => `<option value="${esc(c.url)}"${data.garmin_target_calendar === c.url ? " selected" : ""}>${esc(c.name)}</option>`).join("")}
    </select>` : sources
      ? `<p class="sub"><a href="#/settings/calendar">${t("First sign in to your calendar under Sources and choose a calendar.")}</a></p>`
      : `<p class="empty">${data.connected
        ? t("Choose a calendar above first.")
        : t("No calendars connected. Sign in above to choose a calendar.")}</p>`}
    <p class="sub" data-garmin-target-status role="status" aria-live="polite"></p>
  </div>
  <div class="field">
    <label class="check" for="garmin-mirror-enabled">
      <input type="checkbox" id="garmin-mirror-enabled" aria-describedby="garmin-mirror-help"${data.mirror_enabled ? " checked" : ""}${canCopy(data) ? "" : " disabled"}>
      ${t("Copy Garmin workouts onto this calendar")}
    </label>
    <p class="sub" id="garmin-mirror-help">${mirrorHelp(data)}</p>
    <p class="sub" data-garmin-mirror-status role="status" aria-live="polite"></p>
    <div class="settings-save"><button class="btn" type="button" data-garmin-mirror-preview${canCopy(data) ? "" : " disabled"}>${t("Preview calendar changes")}</button></div>
    <p class="sub" data-garmin-preview-status role="status" aria-live="polite"></p>
    <div class="sub" data-garmin-preview-body></div>
  </div>`;
  let preview = null;
  const previewButton = el.querySelector("[data-garmin-mirror-preview]");
  const previewStatus = el.querySelector("[data-garmin-preview-status]");
  const previewBody = el.querySelector("[data-garmin-preview-body]");

  const renderPreview = (result) => {
    if (!previewBody) return;
    if (!result) {
      previewBody.innerHTML = "";
      return;
    }
    if (result.error) {
      previewBody.innerHTML = `<span class="error-text">${esc(previewError(result.error))}</span>`;
      return;
    }
    const counts = result.counts || {};
    const total = Number(result.total_actions || 0);
    const create = Number(counts.create || 0);
    const update = Number(counts.update || 0);
    const remove = Number(counts.delete || 0);
    const conflict = Number(counts.conflict || 0);
    const lines = [];
    if (!result.ready_for_writes) {
      lines.push(t("Copying is paused until Garmin planning is up to date."));
    }
    if (!result.deletes_allowed && result.unkeyed_future) {
      lines.push(
        t("{n} workouts cannot be matched safely yet. Existing copies will stay.", {
          n: result.unkeyed_future,
        }),
      );
    }
    if (result.ignored_unreadable) {
      lines.push(
        t("{n} calendar items could not be read and will stay untouched.", {
          n: result.ignored_unreadable,
        }),
      );
    }
    if (!total) {
      lines.push(t("No calendar changes are planned."));
    }
    const listed = (result.actions || []).slice(0, 20);
    const actionRows = listed.length
      ? `<ul class="trends">${listed
        .map(
          (action) => `<li><b>${esc(actionWord(action.op))}</b> · ${esc(action.title || t("Workout"))}${action.event_date ? ` · ${esc(userDate(action.event_date))}` : ""}<details><summary>${t("Details")}</summary><code>${esc(action.origin_id || "")}</code></details></li>`,
        )
        .join("")}</ul>`
      : "";
    const truncated = (result.truncated || total > listed.length) ? `<p>${esc(t("Showing the first {n} items.", { n: listed.length }))}</p>` : "";
    previewBody.innerHTML = `<p>${esc(t("Planned changes: {create} new, {update} changed, {delete} removed, {conflict} left untouched.", {
      create,
      update,
      delete: remove,
      conflict,
    }))}</p>${lines.map((line) => `<p>${esc(line)}</p>`).join("")}${truncated}${actionRows}`;
  };

  const loadPreview = async ({ silent = false } = {}) => {
    if (!previewButton) return null;
    previewButton.disabled = true;
    if (!silent && previewStatus) previewStatus.textContent = t("Loading preview…");
    try {
      preview = (await get("caldav/mirror-preview")).data;
      renderPreview(preview);
      if (previewStatus && !silent) previewStatus.textContent = t("Preview updated.");
      return preview;
    } catch (error) {
      preview = null;
      renderPreview(null);
      if (previewStatus) {
        previewStatus.textContent = error.code === "no_target"
          ? t("Choose a calendar first.")
          : t("Could not load preview: {error}", { error: error.message });
      }
      return null;
    } finally {
      previewButton.disabled = !canCopy(data);
      if (!silent && previewButton.isConnected) previewButton.focus();
    }
  };
  const select = el.querySelector("select");
  select?.addEventListener("change", async () => {
    const prior = data.garmin_target_calendar || "";
    const chosen = select.value;
    const message = el.querySelector("[data-garmin-target-status]");
    select.disabled = true;
    try {
      const saved = (await post("caldav/garmin-target", { url: chosen || null })).data;
      data.garmin_target_calendar = saved.garmin_target_calendar;
      data.mirror_enabled = saved.mirror_enabled;
      data.mirror_active = saved.mirror_active;
      const mirrorBox = el.querySelector("#garmin-mirror-enabled");
      if (mirrorBox) {
        mirrorBox.checked = Boolean(saved.mirror_enabled);
        mirrorBox.disabled = !canCopy(data);
      }
      const help = el.querySelector("#garmin-mirror-help");
      if (help) help.textContent = mirrorHelp(data);
      if (previewButton) previewButton.disabled = !canCopy(data);
      preview = null;
      renderPreview(null);
      message.textContent = t("Saved.");
    } catch (error) {
      select.value = prior;
      message.textContent = error.code === "unknown_calendar"
        ? t("Choose a connected calendar and try again.")
        : t("Not saved: {error}", { error: error.message });
    } finally {
      select.disabled = false;
    }
  });
  const mirror = el.querySelector("#garmin-mirror-enabled");
  mirror?.addEventListener("change", async () => {
    const message = el.querySelector("[data-garmin-mirror-status]");
    const enabling = mirror.checked && !data.mirror_enabled;
    mirror.disabled = true;
    try {
      if (enabling) {
        const currentPreview = await loadPreview({ silent: true });
        if (!currentPreview || currentPreview.error) {
          mirror.checked = false;
          message.textContent = t("Preview the changes first, then try again.");
          return;
        }
        const counts = currentPreview.counts || {};
        const confirmText = currentPreview.ready_for_writes
          ? t(
            "Copying will add {create}, change {update} and remove {delete} workouts, including existing copies. Turn on copying?",
            {
              create: Number(counts.create || 0),
              update: Number(counts.update || 0),
              delete: Number(counts.delete || 0),
            },
          )
          : t(
            "Garmin planning is not up to date. Turn on copying so it starts after the next complete sync?",
          );
        if (!window.confirm(confirmText)) {
          mirror.checked = false;
          message.textContent = t("Copying stayed off.");
          return;
        }
      }
      const saved = (await post("caldav/mirror", { enabled: mirror.checked })).data;
      data.mirror_enabled = saved.mirror_enabled;
      data.mirror_active = saved.mirror_active;
      message.textContent = saved.mirror_active
        ? t("Garmin workouts will show up here after the next sync. Your own appointments stay as they are.")
        : t("Copying is off. Your calendar stays as it is.");
    } catch (error) {
      mirror.checked = Boolean(data.mirror_enabled);
      message.textContent = error.code === "read_only_account"
        ? t("Google can only read this calendar, so workouts are not copied there.")
        : error.code === "no_target"
          ? t("Choose a calendar first.")
          : t("Not saved: {error}", { error: error.message });
    } finally {
      mirror.disabled = !canCopy(data);
      if (previewButton) previewButton.disabled = !canCopy(data);
    }
  });
  previewButton?.addEventListener("click", async () => {
    await loadPreview();
  });
}

function canCopy(data) {
  return Boolean(data.garmin_target_calendar) && data.provider !== "google" && data.connected;
}

function mirrorHelp(data) {
  if (data.provider === "google") {
    return t("Google can only read this calendar, so workouts are not copied there.");
  }
  if (!data.garmin_target_calendar) return t("Choose a calendar first. Your own appointments stay untouched.");
  return t("Your own appointments stay untouched. Preview changes to new and existing copies before turning copying on.");
}

function previewError(code) {
  if (code === "calendar_read_failed") return t("Could not read this calendar right now.");
  if (code === "duplicate_mirror") return t("A copied workout appears multiple times; resolve that first.");
  return t("Preview failed.");
}

function actionWord(op) {
  if (op === "create") return t("Create");
  if (op === "update") return t("Update");
  if (op === "delete") return t("Delete");
  return t("Blocked");
}
