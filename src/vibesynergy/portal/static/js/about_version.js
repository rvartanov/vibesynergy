// About this version: the footer line and the Settings → System section.
// Which version runs, since when, the database schema and the last deploy; with updates
// waiting, "Update now" starts the server's update after your password (without a login, the
// coach passphrase); where the server can't update itself, how to do it.
import { get, post } from "./api.js";
import { ago, esc, userDateTime } from "./format.js";
import { t } from "./i18n.js";

// "VibeSynergy 0.1.0 · 7674c57", linking to Settings → System → About this version.
export function versionLinkHtml(info) {
  const parts = [info.version ? `VibeSynergy ${info.version}` : "VibeSynergy", info.commit_short].filter(Boolean);
  return `<a href="#/settings/system?section=about" data-version-link>${esc(parts.join(" · "))}</a>`;
}

function row(label, value) {
  return `<tr><th scope="row">${esc(label)}</th><td>${value}</td></tr>`;
}

// The answer, when it was last checked, and a button to check again now.
function updatesHtml(u) {
  return `${updatesState(u)} <span class="update-check"><button class="btn" type="button" data-update-check>${esc(t("Check for updates"))}</button>${
    u.checked_at ? ` <span class="sub" data-update-checked>${esc(t("Checked {when}", { when: ago(u.checked_at) }))}</span>` : ""
  }</span>`;
}

function updatesState(u) {
  if (!u.behind) return esc(t("Up to date"));
  const titles = (u.titles || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const label = u.count_known === false ? t("An update is available") : t("{n} updates available", { n: u.titles?.length || u.behind });
  return `<span class="chip warning">${esc(label)}</span>${titles ? `<ul class="sub">${titles}</ul>` : ""}`;
}

// Update now: only with updates waiting and an update unit on the server.
function updateHtml(info) {
  const unit = info.update;
  if (!unit) return info.update_hint ? `<p class="sub">${esc(t(info.update_hint))}</p>` : "";
  const log = info.update_log?.tail?.length
    ? `<details class="details"><summary>${esc(t("Last update log"))}</summary><pre class="sub">${esc(info.update_log.tail.join("\n"))}</pre></details>`
    : "";
  if (unit.running) {
    return `<p class="sub" role="status" data-update-status>${esc(t("Updating… The page reloads when the new version answers."))}</p>${log}`;
  }
  const last = unit.result && unit.result !== "success" ? `<p><span class="chip warning">${esc(t("The last update did not succeed."))}</span></p>` : "";
  const form = info.updates?.behind
    ? `<form class="filters" data-update-form>
        <label>${esc(info.confirm === "password" ? t("Your password") : t("Coach passphrase"))} <input class="btn" type="password" name="secret" autocomplete="current-password" required></label>
        <button class="btn primary" type="submit">${esc(t("Update now"))}</button>
      </form>
      <p class="sub">${esc(t("VibeSynergy makes a backup, updates, restarts and checks itself. It takes about a minute."))}</p>`
    : "";
  return `${last}${form}<p class="sub" role="status" data-update-status></p>${log}`;
}

export function aboutHtml(info, { open = false } = {}) {
  const commit = info.commit_short
    ? `<a href="${esc(info.commit_url)}" rel="noopener noreferrer" target="_blank">${esc(info.commit_short)}</a>${info.commit_subject ? ` · ${esc(info.commit_subject)}` : ""}`
    : esc(t("unknown"));
  const schema = info.schema
    ? `${esc(info.schema)}${info.schema_head && info.schema_head !== info.schema ? ` <span class="chip warning">${esc(t("update pending: {rev}", { rev: info.schema_head }))}</span>` : ""}`
    : "–";
  const deploy = info.deploy
    ? `${esc(info.deploy.at ? userDateTime(info.deploy.at) : "–")} · <span class="chip ${info.deploy.result === "ok" ? "success" : "warning"}">${esc(info.deploy.result === "ok" ? t("succeeded") : t("did not succeed"))}</span>${info.deploy.migrated ? ` · ${esc(t("database updated"))}` : ""}`
    : esc(t("No deploy record yet."));
  const summary = t("VibeSynergy {version}, live since {when}", {
    version: [info.version, info.commit_short].filter(Boolean).join(" · ") || "–",
    when: ago(info.started_at),
  });
  return `<details class="card collapsible" id="about" data-about${open ? " open" : ""}>
    <summary>${esc(t("About this version"))} <span class="sub">${esc(summary)}</span></summary>
    <div class="table-wrap"><table aria-label="${esc(t("About this version"))}"><tbody>
      ${row(t("Version"), esc(info.version || "–"))}
      ${row(t("Code"), commit)}
      ${info.commit_date ? row(t("Code date"), esc(userDateTime(info.commit_date))) : ""}
      ${row(t("Running since"), esc(userDateTime(info.started_at)))}
      ${row(t("Database"), schema)}
      ${row(t("Last deploy"), deploy)}
      ${info.updates ? row(t("Updates"), updatesHtml(info.updates)) : ""}
      ${info.local_changes ? row(t("Local changes"), `<span class="chip warning">${esc(t("the server has uncommitted changes"))}</span>`) : ""}
    </tbody></table></div>
    <div data-update>${updateHtml(info)}</div>
  </details>`;
}

export async function mountAbout(el, { open = false } = {}) {
  if (!el) return;
  try {
    const { data } = await get("about");
    if (!el.isConnected) return;
    el.innerHTML = aboutHtml(data, { open });
    if (open) el.querySelector("[data-about]")?.scrollIntoView({ block: "start" });
    bindUpdate(el, data);
  } catch {
    el.innerHTML = "";
  }
}

// After the start: the service restarts, so answers fail for a while. Reload once another
// commit answers; give up after ten minutes and say so.
function bindUpdate(el, info) {
  const status = el.querySelector("[data-update-status]");
  if (info.update?.running) waitForNewVersion(el, info.commit, status);
  const check = el.querySelector("[data-update-check]");
  check?.addEventListener("click", async () => {
    check.disabled = true;
    check.textContent = t("Checking…");
    try {
      const { data } = await post("about/check");
      if (!el.isConnected) return;
      el.innerHTML = aboutHtml(data, { open: true });
      bindUpdate(el, data);
      el.querySelector("[data-update-check]")?.focus();
    } catch (error) {
      check.disabled = false;
      check.textContent = t("Check for updates");
      if (status) status.textContent = t("Could not check: {error}", { error: t(error.message) });
    }
  });
  el.querySelector("[data-update-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const button = form.querySelector("button");
    button.disabled = true;
    status.textContent = t("Starting the update…");
    try {
      const secret = form.secret.value;
      await post("settings/update", info.confirm === "password" ? { password: secret } : { passphrase: secret });
      form.remove();
      status.textContent = t("Updating… The page reloads when the new version answers.");
      waitForNewVersion(el, info.commit, status);
    } catch (error) {
      button.disabled = false;
      status.textContent = t("Not started: {error}", { error: t(error.message) });
      form.secret.focus();
    }
  });
}

function waitForNewVersion(el, commit, status) {
  const until = Date.now() + 10 * 60 * 1000;
  const tick = async () => {
    if (!el.isConnected) return;
    try {
      const { data } = await get("about");
      if (data.commit && data.commit !== commit) return window.location.reload();
      if (data.update && !data.update.running && data.update.result && data.update.result !== "success") {
        status.textContent = t("The update did not succeed. VibeSynergy still runs the old version.");
        return;
      }
    } catch {
      // restarting: try again
    }
    if (Date.now() > until) {
      status.textContent = t("No new version after ten minutes. Check the update log.");
      return;
    }
    setTimeout(tick, 5000);
  };
  setTimeout(tick, 5000);
}
