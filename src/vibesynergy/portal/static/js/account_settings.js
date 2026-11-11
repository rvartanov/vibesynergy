// Settings → Account: the login. Change the password, the optional second factor (a code by
// mail or an authenticator app, with backup codes), the devices you are logged in on, and
// log out here or everywhere. The account's own API lives under /auth.
import { esc, userDateTime } from "./format.js";
import { t } from "./i18n.js";

async function call(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: method === "POST" ? { "content-type": "application/json", "x-vibesynergy": "portal" } : {},
    body: method === "POST" ? JSON.stringify(body || {}) : undefined,
    credentials: "same-origin",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || data.detail || "Something went wrong. Try again.");
  return data;
}

const MFA = { none: "Off", mail: "A code by mail", totp: "An authenticator app" };

function html(a) {
  const sessions = a.sessions
    .map((s) => `<li>${esc(s.agent.slice(0, 60) || t("Unknown device"))} · ${esc(s.client || "?")} · ${esc(t("last used {when}", { when: userDateTime(s.last_seen) }))}${s.current ? ` <span class="chip good">${esc(t("This device"))}</span>` : ` <button class="btn" type="button" data-end-session="${esc(s.id)}">${esc(t("Log out"))}</button>`}</li>`)
    .join("");
  return `<section class="card settings-section" aria-labelledby="account-title">
    <h2 id="account-title">${esc(t("Account"))}</h2>
    <p>${esc(a.email)}</p>
    <h3 class="settings-subhead">${esc(t("Password"))}</h3>
    <form data-account-password novalidate class="settings-grid">
      ${a.password_set ? `<label for="account-current">${esc(t("Current password"))}</label><input class="btn" id="account-current" type="password" autocomplete="current-password" required>` : ""}
      <label for="account-new">${esc(t("New password"))}</label>
      <input class="btn" id="account-new" type="password" autocomplete="new-password" minlength="12" required aria-describedby="account-new-hint">
      <p class="sub" id="account-new-hint">${esc(t("At least 12 characters. A short sentence works well."))}</p>
      <div class="settings-save"><button class="btn" type="submit">${esc(t("Change password"))}</button></div>
    </form>
    <h3 class="settings-subhead">${esc(t("Second step when logging in"))}</h3>
    <p>${esc(t("Now: {method}", { method: t(MFA[a.mfa] || a.mfa) }))}${a.mfa === "totp" ? ` · ${esc(t("{n} backup codes left", { n: a.backup_codes_left }))}` : ""}</p>
    ${a.password_set ? `<div class="settings-grid"><label for="account-mfa-current">${esc(t("Your password, to confirm the change"))}</label><input class="btn" id="account-mfa-current" type="password" autocomplete="current-password"></div>` : ""}
    <div class="settings-save" data-mfa-choices>
      ${a.mfa !== "totp" ? `<button class="btn" type="button" data-mfa="totp">${esc(t("Use an authenticator app"))}</button>` : ""}
      ${a.mfa !== "mail" ? `<button class="btn" type="button" data-mfa="mail"${a.mail_configured ? "" : " disabled aria-describedby=\"account-mail-hint\""}>${esc(t("Use a code by mail"))}</button>` : ""}
      ${a.mfa !== "none" ? `<button class="btn" type="button" data-mfa="off">${esc(t("Turn off"))}</button>` : ""}
    </div>
    ${a.mail_configured ? "" : `<p class="sub" id="account-mail-hint">${esc(t("A code by mail needs mail to be set up (Settings → Alerts)."))}</p>`}
    <div data-totp-setup></div>
    <h3 class="settings-subhead">${esc(t("Where you are logged in"))}</h3>
    <ul class="plain">${sessions}</ul>
    <div class="settings-save">
      <button class="btn" type="button" data-logout>${esc(t("Log out"))}</button>
      <button class="btn" type="button" data-logout-all>${esc(t("Log out everywhere"))}</button>
    </div>
    <p class="sub" role="status" aria-live="polite" data-account-status></p>
  </section>`;
}

export async function mountAccount(host) {
  if (!host) return;
  let data;
  try {
    data = await call("GET", "/auth/account");
  } catch (error) {
    host.innerHTML = `<section class="card"><p class="sub">${esc(t(error.message))}</p></section>`;
    return;
  }
  host.innerHTML = html(data);
  const status = host.querySelector("[data-account-status]");
  const say = (text, error = false) => {
    status.textContent = t(text);
    status.setAttribute("role", error ? "alert" : "status");
  };
  const redraw = () => mountAccount(host);
  const confirmPassword = () => host.querySelector("#account-mfa-current")?.value || null;

  host.querySelector("[data-account-password]").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await call("POST", "/auth/password", {
        password: host.querySelector("#account-new").value,
        current: host.querySelector("#account-current")?.value || null,
      });
      say("Password changed. Other devices are logged out.");
      event.target.reset();
    } catch (error) {
      say(error.message, true);
    }
  });

  host.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    try {
      if (button.dataset.mfa === "totp") {
        const setup = await call("POST", "/auth/totp/start", { current: confirmPassword() });
        const box = host.querySelector("[data-totp-setup]");
        box.innerHTML = `<p>${esc(t("Add this key to your authenticator app, then enter the code it shows."))}</p>
          <p><code data-totp-secret>${esc(setup.secret.match(/.{1,4}/g).join(" "))}</code></p>
          <p class="sub"><a href="${esc(setup.uri)}">${esc(t("Open in the app on this device"))}</a></p>
          <form data-totp-confirm novalidate class="settings-grid">
            <label for="totp-code">${esc(t("Code"))}</label>
            <input class="btn" id="totp-code" inputmode="numeric" autocomplete="one-time-code" required>
            <div class="settings-save"><button class="btn" type="submit">${esc(t("Turn on"))}</button></div>
          </form>`;
        box.querySelector("[data-totp-confirm]").addEventListener("submit", async (e) => {
          e.preventDefault();
          try {
            const done = await call("POST", "/auth/totp/confirm", { code: box.querySelector("#totp-code").value });
            box.innerHTML = `<p>${esc(t("Done. Keep these backup codes somewhere safe; each works once if you lose your phone."))}</p>
              <ul class="plain backup-codes">${done.backup_codes.map((c) => `<li><code>${esc(c)}</code></li>`).join("")}</ul>
              <button class="btn" type="button" data-refresh>${esc(t("I have saved them"))}</button>`;
          } catch (error) {
            say(error.message, true);
          }
        });
        box.querySelector("#totp-code").focus();
      } else if (button.dataset.mfa === "mail") {
        await call("POST", "/auth/mfa/mail", { on: true, current: confirmPassword() });
        say("From now on you get a code by mail when you log in.");
        redraw();
      } else if (button.dataset.mfa === "off") {
        await call("POST", "/auth/mfa/mail", { on: false, current: confirmPassword() });
        say("The second step is off.");
        redraw();
      } else if (button.dataset.refresh !== undefined) {
        redraw();
      } else if (button.dataset.endSession) {
        await call("POST", "/auth/sessions/end", { id: button.dataset.endSession });
        redraw();
      } else if (button.dataset.logout !== undefined) {
        await call("POST", "/auth/logout");
        location.assign("/login");
      } else if (button.dataset.logoutAll !== undefined) {
        await call("POST", "/auth/logout-all");
        location.assign("/login");
      }
    } catch (error) {
      say(error.message, true);
    }
  });
}
