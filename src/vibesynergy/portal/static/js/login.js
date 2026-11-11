// The login page (/login): sign in with a password or a link by mail, a second factor when
// one is set, setting the first password, and choosing a new one after a reset link.
// Tokens from mail arrive after "#" and are posted, never sent in a URL to the server.
import { N, setLanguage, t } from "./i18n.js";

const host = document.querySelector("[data-login]");
const title = document.querySelector("[data-login-title]");
const status = document.querySelector("[data-login-status]");

async function send(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json", "x-vibesynergy": "portal" },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || data.detail || "Something went wrong. Try again.");
  return data;
}

function say(text, error = false) {
  status.textContent = text ? t(text) : "";
  status.setAttribute("role", error ? "alert" : "status");
}

function next() {
  const wanted = new URLSearchParams(location.search).get("next") || "/";
  // Only paths on this site: never an absolute or protocol-relative address.
  return wanted.startsWith("/") && !wanted.startsWith("//") ? wanted : "/";
}

function field(id, label, type, autocomplete, extra = "") {
  return `<label for="${id}">${t(label)}</label>
    <input class="btn" id="${id}" name="${id}" type="${type}" autocomplete="${autocomplete}" required ${extra}>`;
}

const VIEWS = {
  signin: {
    title: N("Log in"),
    html: () => `<form data-form="signin" novalidate>
      ${field("email", N("Email address"), "email", "username", 'inputmode="email"')}
      ${field("password", N("Password"), "password", "current-password")}
      <button class="btn primary" type="submit">${t("Log in")}</button>
    </form>
    <p><button class="btn link" type="button" data-go="link">${t("Log in with a link by mail")}</button></p>
    <p><button class="btn link" type="button" data-go="forgot">${t("Forgot your password?")}</button></p>`,
  },
  link: {
    title: N("Log in with a link"),
    html: () => `<p class="sub">${t("We mail you a link that logs you in. It works once, for 15 minutes.")}</p>
    <form data-form="link" novalidate>
      ${field("email", N("Email address"), "email", "username", 'inputmode="email"')}
      <button class="btn primary" type="submit">${t("Send the link")}</button>
    </form>
    <p><button class="btn link" type="button" data-go="signin">${t("Back to log in")}</button></p>`,
  },
  forgot: {
    title: N("Forgot your password?"),
    html: () => `<p class="sub">${t("We mail you a link to choose a new password. It works once, for 15 minutes.")}</p>
    <form data-form="forgot" novalidate>
      ${field("email", N("Email address"), "email", "username", 'inputmode="email"')}
      <button class="btn primary" type="submit">${t("Send the link")}</button>
    </form>
    <p><button class="btn link" type="button" data-go="signin">${t("Back to log in")}</button></p>`,
  },
  mfa: {
    title: N("One more step"),
    html: (method) => `<p class="sub">${t(method === "mail" ? "We mailed you a code. Enter it here." : "Enter the code from your authenticator app, or one of your backup codes.")}</p>
    <form data-form="mfa" novalidate>
      ${field("code", N("Code"), "text", "one-time-code", 'inputmode="numeric" autocapitalize="off" spellcheck="false"')}
      <button class="btn primary" type="submit">${t("Continue")}</button>
    </form>`,
  },
  password: {
    title: N("Choose your password"),
    html: () => `<p class="sub">${t("At least 12 characters. A short sentence works well.")}</p>
    <form data-form="password" novalidate>
      ${field("password", N("New password"), "password", "new-password", 'minlength="12"')}
      <button class="btn primary" type="submit">${t("Save and continue")}</button>
    </form>`,
  },
  reset: {
    title: N("Choose a new password"),
    html: () => `<p class="sub">${t("At least 12 characters. A short sentence works well.")}</p>
    <form data-form="reset" novalidate>
      ${field("password", N("New password"), "password", "new-password", 'minlength="12"')}
      <button class="btn primary" type="submit">${t("Save and log in")}</button>
    </form>`,
  },
};

let resetToken = null;

function show(view, arg) {
  title.textContent = t(VIEWS[view].title);
  host.innerHTML = VIEWS[view].html(arg);
  host.querySelector("input")?.focus();
}

async function after(result) {
  if (result.next === "mfa") return show("mfa", result.method);
  if (result.next === "password") return show("password");
  location.replace(next());
}

host.addEventListener("click", (event) => {
  const go = event.target.closest("[data-go]");
  if (go) {
    say("");
    show(go.dataset.go);
  }
});

host.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const value = (name) => form.elements[name]?.value || "";
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  say("");
  try {
    const kind = form.dataset.form;
    if (kind === "signin") await after(await send("/auth/login", { email: value("email"), password: value("password") }));
    else if (kind === "link" || kind === "forgot") {
      await send(kind === "link" ? "/auth/link" : "/auth/reset", { email: value("email") });
      say(kind === "link" ? "If this address belongs to the account, a link is on its way." : "If this address belongs to the account, a link to choose a new password is on its way.");
    } else if (kind === "mfa") await after(await send("/auth/mfa", { code: value("code") }));
    else if (kind === "password") {
      await send("/auth/password", { password: value("password") });
      location.replace(next());
    } else if (kind === "reset") await after(await send("/auth/reset/use", { token: resetToken, password: value("password") }));
  } catch (error) {
    say(error.message, true);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
});

async function start() {
  await setLanguage("auto");
  const hash = new URLSearchParams(location.hash.slice(1));
  history.replaceState(null, "", location.pathname + location.search); // the token leaves the address bar
  if (hash.get("link")) {
    show("signin");
    say("Logging you in…");
    try {
      await after(await send("/auth/link/use", { token: hash.get("link") }));
      say("");
    } catch (error) {
      say(error.message, true);
    }
    return;
  }
  if (hash.get("reset")) {
    resetToken = hash.get("reset");
    return show("reset");
  }
  const state = await (await fetch("/auth/state", { credentials: "same-origin" })).json();
  if (state.logged_in && state.password_set === false) return show("password");
  if (state.logged_in) return location.replace(next());
  if (state.mfa_pending) return show("mfa", state.method);
  show("signin");
}

start();
