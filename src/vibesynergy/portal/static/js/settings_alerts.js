// Settings → Alerts (split from pages/settings.js): email, the alert groups, mail
// settings and where each kind of alert goes (portal, email, push).
import { post } from "./api.js";
import { esc, number } from "./format.js";
import { N, t } from "./i18n.js";
import { reportSaveError } from "./settings_autosave.js";
import { field, saveRow, select } from "./settings_pieces.js";

// [id, label, kinds, hint]. A logged complaint (injury) is never emailed, whatever the route.
export const ALERT_GROUPS = [
  ["health", N("Health warnings"), ["injury", "health"], N("A red day, low HRV or heavy load.")],
  ["sync", N("Sync problems"), ["login", "silent", "job", "history", "import"], N("A login that stopped working, a quiet source, a failed import.")],
  ["coach", N("Messages from your coach"), ["daily_coach", "token"], N("A new morning note, and a coach token about to expire.")],
];

export function alertGroupsHtml(a) {
  const routes = a.routes || {};
  const boxes = ALERT_GROUPS.map(([id, label, kinds, hint]) => {
    const on = kinds.every((kind) => (routes[kind] || []).includes("email"));
    return `<label class="check"><input type="checkbox" data-alert-group="${esc(id)}"${on ? " checked" : ""}> <span>${esc(t(label))}<span class="sub alert-group-hint">${esc(t(hint))}</span></span></label>`;
  }).join("");
  return `<section class="card" data-alert-groups>
    <h2>${t("What to email you about")}</h2>
    <p class="sub">${t("The banner at the top of the portal shows your alerts. Tick what should also reach you by email.")}</p>
    <fieldset class="settings-fieldset"><legend class="visually-hidden">${t("What to email you about")}</legend>
      <div class="settings-grid">${boxes}</div>
    </fieldset>
    <p class="sub" data-groups-status role="status" aria-live="polite"></p>
  </section>`;
}

// The group boxes and the per-kind table write the same routes; keep both in view.
export function showRoutes(view, routes) {
  for (const box of view.querySelectorAll("[data-route]")) {
    box.checked = (routes[box.dataset.route] || []).includes(box.value);
  }
  for (const box of view.querySelectorAll("[data-alert-group]")) {
    const group = ALERT_GROUPS.find(([id]) => id === box.dataset.alertGroup);
    if (group) box.checked = group[2].every((kind) => (routes[kind] || []).includes("email"));
  }
}

export function bindRoutes(el, view, ctx) {
  const status = el.querySelector("[data-routes-status]");
  const save = async (routes) => {
    status.textContent = t("Saving…");
    try {
      await post("prefs", { changes: { alert_routes: routes } });
      if (routes && ctx.s?.alerts) {
        ctx.s.alerts.routes = routes;
        showRoutes(view, routes);
      }
      status.textContent = t("Saved.");
      window.dispatchEvent(new Event("vibesynergy-alerts"));
    } catch (error) {
      status.textContent = t("Not saved: {error}", { error: error.message });
    }
  };
  el.addEventListener("change", () => {
    const routes = {};
    for (const box of el.querySelectorAll("[data-route]")) {
      routes[box.dataset.route] ||= [];
      if (box.checked) routes[box.dataset.route].push(box.value);
    }
    save(routes);
  });
  el.querySelector("[data-routes-reset]").addEventListener("click", async () => {
    await save(null);
    location.reload();
  });
}

export function mailHtml(m) {
  return `<section class="card settings-section" data-section="mail" aria-label="${esc(t("Mail settings"))}">
    <p class="sub">${t("The mail account VibeSynergy sends from. Any provider works. With IMAP filled in, a copy lands in your Sent folder.")}</p>
    <div class="settings-grid">
      ${field(t("SMTP server"), "smtp_host", m.smtp_host, 'placeholder="smtp.example.com"')}
      ${field(t("SMTP port"), "smtp_port", m.smtp_port, 'type="number" placeholder="587"')}
      <label>${t("Security")} ${select("smtp_security", [["starttls", "STARTTLS (587)"], ["ssl", "SSL/TLS (465)"], ["none", t("None (25)")]], m.smtp_security || "starttls")}</label>
      ${field(t("Username"), "username", m.username, 'autocomplete="off"')}
      ${field(t("From address"), "from", m.from, `placeholder="${esc(t("same as username"))}"`)}
      ${field(t("IMAP server (optional)"), "imap_host", m.imap_host, 'placeholder="imap.example.com"')}
      ${field(t("IMAP port"), "imap_port", m.imap_port, 'type="number" placeholder="993"')}
      ${field(t("Sent folder"), "sent_folder", m.sent_folder, 'placeholder="Sent"')}
    </div>
    ${saveRow()}
    <div class="settings-grid">
      <label>${t("Password")} ${m.password_set ? t("(set)") : t("(not set)")} <input class="btn" type="password" name="password" autocomplete="new-password" placeholder="${esc(m.password_set ? t("enter to replace") : t("app password"))}"></label>
    </div>
    <p class="sub" data-secret-status role="status" aria-live="polite"></p>
    <p class="sub">${t("Stored only on this server, never shown again. For iCloud, Gmail and others with two-step login, use an app-specific password.")}</p>
  </section>`;
}

export function emailState(a, m) {
  if (a.channels.email) return ["good", t("ready")];
  if (!a.email_to) return ["warning", t("needs an address")];
  if (!m.smtp_host || !m.username || !m.password_set) return ["warning", t("needs mail settings")];
  return ["warning", t("not ready")];
}

export function alertsHtml(a, m) {
  const [tone, state] = emailState(a, m);
  const open = a.open.length
    ? `<ul class="trends">${a.open.map((o) => `<li>${esc(o.text)}</li>`).join("")}</ul>`
    : `<p class="sub">${t("Nothing needs attention now.")}</p>`;
  return `<section class="card settings-section" data-section="alerts">
    <h2>${t("Email")} <span class="chip ${tone}" data-email-state>${esc(state)}</span></h2>
    <p class="sub">${esc(t("VibeSynergy checks every {n} minutes and emails you when something needs action. The same warning waits a day.", { n: number(a.every_minutes) }))}</p>
    <div class="settings-grid">
      ${field(t("Email alerts to"), "email_to", a.email_to, 'type="email" autocomplete="email" placeholder="you@example.com"')}
      <label class="check"><input type="checkbox" name="enabled"${a.enabled === false ? "" : " checked"} aria-describedby="alerts-enabled-hint"> <span>${t("Alerts on")}<span class="sub alert-group-hint" id="alerts-enabled-hint">${t("Off: VibeSynergy stops checking and sends no email.")}</span></span></label>
    </div>
    ${saveRow()}
    <div class="settings-save"><button class="btn primary" type="button" data-test-alert>${t("Send test alert")}</button><span class="sub" data-test-status role="status" aria-live="polite"></span></div>
    <h3>${t("Open now")}</h3>${open}
  </section>`;
}

// Kinds x channels: where each kind of alert goes. Saved at once (no restart needed).
export const CHANNEL_NAMES = [["portal", N("Portal")], ["email", N("Email")], ["push", N("Push")]];

export function routesHtml(a) {
  const rows = a.kinds
    .map(
      (k) => `<tr><td>${esc(t(k.label))}</td>${CHANNEL_NAMES.map(
        ([c, name]) => `<td class="num"><input type="checkbox" data-route="${esc(k.id)}" value="${c}" aria-label="${esc(t(k.label))}: ${t(name)}"${a.routes[k.id]?.includes(c) ? " checked" : ""}></td>`,
      ).join("")}</tr>`,
    )
    .join("");
  return `<section class="card" data-routes>
    <p class="sub">${t("Fine-tune each kind. Portal is the banner at the top of every page. A complaint you logged yourself stays in the portal. Saved at once.")}</p>
    <div class="table-wrap"><table><thead><tr><th>${t("Kind")}</th>${CHANNEL_NAMES.map(([, name]) => `<th class="num">${t(name)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>
    <div class="settings-save"><button class="btn" type="button" data-routes-reset>${t("Back to the defaults")}</button><span class="sub" data-routes-status role="status" aria-live="polite"></span></div>
  </section>`;
}

export function syncEmailState(view, data) {
  const chip = view.querySelector("[data-email-state]");
  if (!chip || !data?.alerts || !data?.mail) return;
  const [tone, state] = emailState(data.alerts, data.mail);
  chip.className = `chip ${tone}`;
  chip.textContent = state;
}

export function bindAlertGroups(view, ctx) {
  const root = view.querySelector("[data-alert-groups]");
  if (!root || !ctx.s?.alerts) return;
  const status = root.querySelector("[data-groups-status]");
  root.addEventListener("change", async (event) => {
    const box = event.target.closest("[data-alert-group]");
    if (!box) return;
    const group = ALERT_GROUPS.find(([id]) => id === box.dataset.alertGroup);
    if (!group) return;
    const next = { ...ctx.s.alerts.routes };
    for (const kind of group[2]) {
      const channels = new Set(next[kind] || []);
      if (box.checked) channels.add("email");
      else channels.delete("email");
      next[kind] = [...channels];
    }
    try {
      await post("prefs", { changes: { alert_routes: next } });
      ctx.s.alerts.routes = next;
      showRoutes(view, next);
      if (status) status.textContent = t("Saved.");
      window.dispatchEvent(new Event("vibesynergy-alerts"));
    } catch {
      box.checked = !box.checked;
      reportSaveError(status, () => box.dispatchEvent(new Event("change", { bubbles: true })));
    }
  });
}
