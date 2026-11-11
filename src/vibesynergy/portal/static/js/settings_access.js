// Settings → Sharing: coach apps, tokens and the access log, split from
// pages/settings.js. `draw` redraws the whole Settings page after a change.
import { get, post } from "./api.js";
import { ago, esc, userDateTime } from "./format.js";
import { N, t } from "./i18n.js";

const SCOPE_LABELS = [
  N("See my training and health data"),
  N("Search my history in detail"),
  N("Log check-ins, notes and pain for me"),
  N("Write messages to me"),
  N("Suggest changes to my plan"),
  N("Change my intervals.icu plan"),
  N("Fetch new data from my accounts"),
  N("Publish stats to Strava or Garmin"),
  N("Run read-only database queries"),
];

// Read from the rows of apps, tokens and share links: the lists show only what works.
const ACCESS_VERBS = {
  "access.app_revoked": N("app access revoked"),
  "access.token_revoked": N("token revoked"),
  "access.token_expired": N("token expired"),
  "access.link_revoked": N("share link revoked"),
  "access.link_expired": N("share link expired"),
};
const ACTIVITY_VERBS = {
  vibesynergy_coach_write: N("wrote advice"),
  vibesynergy_coach_entries: N("read coach notes"),
  vibesynergy_prediction_scores: N("checked predictions"),
  vibesynergy_coach_log: N("wrote a note"),
  vibesynergy_model_run_write: N("saved model settings"),
  vibesynergy_model_runs: N("looked up model settings"),
  vibesynergy_add_note: N("logged a note"),
  vibesynergy_edit_note: N("changed a note"),
  vibesynergy_checkin: N("logged a check-in"),
  vibesynergy_log_injury: N("logged pain"),
  vibesynergy_icu_create_event: N("added a plan session"),
  vibesynergy_icu_update_event: N("changed a plan session"),
  vibesynergy_icu_move_event: N("moved a plan session"),
  vibesynergy_icu_delete_event: N("removed a plan session"),
  vibesynergy_icu_revert: N("undid a plan change"),
  vibesynergy_icu_resolve_duplicate: N("chose which plan to keep"),
  vibesynergy_kb_search: N("searched knowledge"),
  vibesynergy_kb_get: N("opened a knowledge article"),
  vibesynergy_kb_upsert: N("updated knowledge"),
  vibesynergy_kb_propose_change: N("suggested a knowledge change"),
  vibesynergy_coach_threshold_propose: N("suggested a threshold change"),
  vibesynergy_refresh: N("fetched new data"),
  vibesynergy_activity_publish: N("published stats"),
  vibesynergy_activity_unpublish: N("removed published stats"),
  vibesynergy_sql: N("ran a database query"),
};

function scopeChecks(access) {
  const chosen = new Set(access.coach_scopes || []);
  return (access.scopes || [])
    .map(
      (scope) => `<label class="check"><input type="checkbox" name="scope" value="${esc(scope.id)}"${chosen.has(scope.id) ? " checked" : ""}> ${esc(t(scope.label))}</label>`,
    )
    .join("");
}

// A token that expired works no more than a revoked one, so both leave the main list.
function tokenEnded(row, now = Date.now()) {
  return Boolean(row.revoked) || (row.expires_at && Date.parse(row.expires_at) <= now);
}

export function coachAppsHtml(access) {
  const allApps = access.clients || [];
  const allTokens = access.tokens || [];
  const apps = allApps.filter((c) => !c.revoked);
  const tokens = allTokens.filter((row) => !tokenEnded(row));
  const appRows = apps.length
    ? apps.map((c) => `<tr>
        <td><input class="btn" data-rename-client="${esc(c.client_id)}" value="${esc(c.display_name || c.name || c.client_id)}" aria-label="${esc(t("Name for {app}", { app: c.name || c.client_id }))}"></td>
        <td>${esc(c.redirect || "–")}</td>
        <td>${c.approved ? esc(ago(c.approved)) : `<span class="muted">${t("not approved")}</span>`}</td>
        <td><button class="btn" type="button" data-revoke="${esc(c.client_id)}" data-name="${esc(c.display_name || c.name || c.client_id)}">${t("Revoke")}</button></td>
      </tr>`).join("")
    : `<tr><td colspan="4">${esc(t(allApps.length ? N("No apps have access now.") : N("No apps have connected yet.")))}</td></tr>`;
  const tokenRows = tokens.length
    ? tokens.map((row) => `<tr>
        <td>${esc(row.display_name || row.name)} <span class="sub">${esc(row.prefix)}</span>${row.stale ? ` <span class="chip warning">${esc(t("Not used for a long time"))}</span>` : ""}</td>
        <td>${esc((row.scopes || []).join(", "))}</td>
        <td>${row.expires_at ? esc(userDateTime(row.expires_at)) : "–"}</td>
        <td>${row.last_used ? esc(userDateTime(row.last_used)) : esc(t("not used yet"))}</td>
        <td><button class="btn" type="button" data-revoke-pat="${esc(row.id)}" data-name="${esc(row.display_name || row.name)}">${t("Revoke")}</button></td>
      </tr>`).join("")
    : `<tr><td colspan="5">${esc(t(allTokens.length ? N("No tokens have access now.") : N("No tokens yet.")))}</td></tr>`;
  return `
    <section class="card" data-coach-apps>
      <h2>${t("Coach apps")}</h2>
      <p class="sub">${t("Connect a coach here. It uses this address and a passphrase you choose. No command is needed.")}</p>
      <p><label>${t("Coach address")}<br><input class="btn wide" readonly value="${esc(access.mcp_url || t("Set the public address first."))}"></label></p>
      <h3 class="settings-subhead">${t("Passphrase")}</h3>
      <p class="sub" role="status">${access.passphrase_set ? t("A passphrase is set.") : t("No passphrase yet.")}</p>
      <form data-passphrase>
        ${access.passphrase_set ? `<label>${t("Current passphrase")} <input class="btn" type="password" name="current" autocomplete="current-password" required></label>` : ""}
        <label>${t("New passphrase")} <input class="btn" type="password" name="new" autocomplete="new-password" minlength="12" required></label>
        <label>${t("Again")} <input class="btn" type="password" name="again" autocomplete="new-password" minlength="12" required></label>
        <p><button class="btn" type="submit">${t("Save passphrase")}</button></p>
        <p class="sub" data-passphrase-status role="status"></p>
      </form>
      <h3 class="settings-subhead">${t("Connected apps")}</h3>
      <div class="table-wrap"><table><thead><tr><th>${t("App")}</th><th>${t("Returns to")}</th><th>${t("Approved")}</th><th>${t("Action")}</th></tr></thead><tbody>${appRows}</tbody></table></div>
    </section>
    <section class="card" data-pat-card>
      <h2>${t("Personal tokens")}</h2>
      <p class="sub">${t("A token expires. Choose 30, 90 or 365 days. It is shown once.")}</p>
      <form data-pat>
        <div class="settings-grid">
          <label>${t("Name")} <input class="btn" name="name" required maxlength="80"></label>
          <label>${t("Expires")} <select class="btn" name="days">
            <option value="30">${t("30 days")}</option>
            <option value="90" selected>${t("90 days")}</option>
            <option value="365">${t("365 days")}</option>
          </select></label>
        </div>
        <details>
          <summary>${t("What this token may do")}</summary>
          <fieldset>
            <legend>${t("What this token may do")}</legend>
            ${scopeChecks(access)}
          </fieldset>
        </details>
        <div class="settings-grid"><label>${t("Your coach passphrase")} <input class="btn" type="password" name="pat_passphrase" autocomplete="current-password" required aria-describedby="pat-passphrase-hint"></label></div>
        <p class="sub" id="pat-passphrase-hint">${access.passphrase_set ? t("A token lets an app into your data, so confirm it with your passphrase.") : t("Set the coach passphrase above first. A token lets an app into your data.")}</p>
        <p><button class="btn" type="submit">${t("Create token")}</button></p>
        <p class="sub" data-pat-status role="status"></p>
      </form>
      <div data-revealed></div>
      <div class="table-wrap"><table><thead><tr><th>${t("Token")}</th><th>${t("Permissions")}</th><th>${t("Expires")}</th><th>${t("Last used")}</th><th>${t("Action")}</th></tr></thead><tbody>${tokenRows}</tbody></table></div>
    </section>`;
}

export function logHtml() {
  return `<section class="card" data-log>
      <h2>${t("Log")}</h2>
      <p class="sub">${t("What coach apps did, and when an app, token or link stopped working.")}</p>
      <div data-activity class="sub">${t("Loading…")}</div>
    </section>`;
}

export function bindCoachApps(view, ctx, draw) {
  const { stale } = ctx;
  const root = view.querySelector("[data-coach-apps]");
  if (!root) return;
  const refresh = (reply, flash) => {
    if (stale()) return;
    draw(view, { ...ctx, clients: reply.data.clients, access: reply.data }, flash || "");
  };
  root.querySelector("[data-passphrase]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const status = root.querySelector("[data-passphrase-status]");
    const next = form.querySelector("[name=new]").value;
    const again = form.querySelector("[name=again]").value;
    if (next !== again) {
      status.textContent = t("The two entries differ. Nothing was saved.");
      return;
    }
    try {
      const current = form.querySelector("[name=current]");
      await post("mcp/passphrase", { new: next, current: current ? current.value : null });
      form.reset();
      const reply = await get("mcp_clients");
      refresh(reply, t("Passphrase saved."));
    } catch (error) {
      status.textContent = t(error.message);
    }
  });
  view.querySelector("[data-pat]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const status = view.querySelector("[data-pat-status]");
    const scopes = [...form.querySelectorAll("[name=scope]:checked")].map((box) => box.value);
    if (!scopes.length) {
      status.textContent = t("Choose at least one permission.");
      return;
    }
    const secret = form.querySelector("[name=pat_passphrase]");
    secret.removeAttribute("aria-invalid");
    try {
      const reply = await post("mcp/pats", {
        name: form.querySelector("[name=name]").value.trim(),
        days: Number(form.querySelector("[name=days]").value),
        scopes,
        passphrase: secret.value,
      });
      const token = reply.data.token;
      refresh(reply, t("Token created. Copy it now. It will not be shown again."));
      const slot = view.querySelector("[data-revealed]");
      if (slot && token) {
        slot.innerHTML = `<p role="status">${esc(t("Copy this token now. VibeSynergy will not show it again."))}</p><p><input class="btn wide" readonly value="${esc(token)}" aria-label="${esc(t("New token"))}"></p>`;
      }
    } catch (error) {
      status.textContent = t(error.message);
      // A passphrase problem: mark the field and put the cursor back in it.
      if (/passphrase|attempts/i.test(error.message)) {
        secret.value = "";
        secret.setAttribute("aria-invalid", "true");
        secret.focus();
      }
    }
  });
  view.querySelectorAll("[data-revoke-pat]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm(t("Revoke {name}? It loses access to VibeSynergy at once.", { name: button.dataset.name }))) return;
      try {
        const reply = await post("mcp/pats/revoke", { id: button.dataset.revokePat });
        refresh(reply, t("Revoked {name}.", { name: button.dataset.name }));
      } catch (error) {
        view.querySelector("[data-pat-status]").textContent = t(error.message);
      }
    });
  });
  root.querySelectorAll("[data-rename-client]").forEach((input) => {
    input.addEventListener("change", async () => {
      try {
        const reply = await post("mcp_clients/rename", {
          client_id: input.dataset.renameClient,
          display_name: input.value.trim(),
        });
        refresh(reply, t("Saved."));
      } catch (error) {
        input.setAttribute("aria-invalid", "true");
        root.querySelector("[data-passphrase-status]").textContent = t(error.message);
      }
    });
  });
  loadActivity(view, accessClientId(ctx), stale);
}

// The log card after a change elsewhere on the page (a revoked share link).
export function refreshLog(stale) {
  const view = document.querySelector("[data-log]")?.closest("#view") || document;
  loadActivity(view, "", stale);
}

function accessClientId(ctx) {
  return ctx.activityClient || "";
}

async function loadActivity(root, clientId, stale) {
  const slot = root.querySelector("[data-activity]");
  if (!slot) return;
  try {
    const reply = await get("mcp/activity", clientId ? { client_id: clientId } : {});
    if (stale() || !slot.isConnected) return;
    const events = reply.data.events || [];
    if (!events.length) {
      slot.innerHTML = `<p class="empty">${esc(t("No coach activity yet."))}</p>`;
      return;
    }
    slot.innerHTML = `<ul class="activity-log">${events.map((event) => {
      const verb = t(ACTIVITY_VERBS[event.action] || ACCESS_VERBS[event.action] || N("used a tool"));
      const who = event.author || event.client_id || "";
      const when = event.at ? userDateTime(event.at) : "";
      return `<li>${esc(when)} · ${esc(who)} · ${esc(verb)}</li>`;
    }).join("")}</ul>`;
  } catch (error) {
    if (!stale() && slot.isConnected) slot.textContent = t(error.message);
  }
}
