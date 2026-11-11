// Settings → Calendar: connect an iCal / CalDAV account (any provider), pick calendars,
// disconnect. Passwords, tokens and Google client secrets are write-only: the server only
// answers with flags. Each step moves focus to its heading; errors are announced and linked
// to the fields with aria-describedby. All texts go through t() (NL + EN).
import { get, post } from "./api.js";
import { ago, esc } from "./format.js";
import { N, t } from "./i18n.js";
import { syncAdvice, syncDetails } from "./sync_error.js";

// The server sends a stable error code; the text is chosen here.
const ERRORS = {
  auth_failed: N("The server refused the username or password. For iCloud, Fastmail and others with two-step login, use an app-specific password."),
  not_caldav: N("This address does not answer as a CalDAV server. Check the address."),
  network: N("Could not reach the server. Check the address and the connection, then try again."),
  insecure_url: N("Only secure https:// addresses are accepted."),
  bad_url: N("Enter a full https:// address, for example https://cloud.example.com/remote.php/dav/."),
  redirect: N("The server redirected to an address VibeSynergy does not trust, so the password was not sent there."),
  missing_fields: N("Enter the username and the password."),
  unknown_calendar: N("That calendar is not in the list found for this account. Choose again from the list."),
  no_selection: N("Select at least one calendar."),
  not_connected: N("Sign in first."),
  bad_provider: N("Choose a provider from the list."),
  reauth: N("Google access has ended. Sign in with Google again."),
  no_google_app: N("Save the Google client ID and secret first."),
  bad_google_app: N("Enter both the Google client ID and the client secret."),
  google_denied: N("Google sign-in was cancelled."),
  google_failed: N("Google did not accept the sign-in. Check the client ID and secret, then try again."),
  state_invalid: N("The Google sign-in expired or did not match. Start it again."),
};

const PROVIDER_HINTS = {
  icloud: N("Create an app-specific password at account.apple.com under Sign-In and Security. Your normal Apple password does not work."),
  fastmail: N("Create an app password in Fastmail under Settings, Privacy & Security, Integrations."),
  other: N("Use the CalDAV address from your provider, for example Nextcloud: https://cloud.example.com/remote.php/dav/. An app password is best."),
};

function failure(error) {
  const text = ERRORS[error.code];
  return text ? t(text) : t("Something went wrong: {error}", { error: error.message });
}

export async function caldavSettings(el, { stale, query }) {
  let status;
  let step = "loading";
  let notice = "";
  let errorCode = "";
  let focusNext = true;
  let chosenProvider = "icloud";
  let draft = {}; // what was typed (never the password), kept when a step is redrawn

  async function load() {
    status = (await get("caldav")).data;
    step = status.connected ? (status.selected.length ? "connected" : "choose") : "signin";
    chosenProvider = status.provider || "icloud";
    const google = query?.get("google");
    if (google === "ok") notice = t("Signed in with Google. Choose the calendars to read.");
    else if (google) {
      errorCode = google;
      step = "signin";
      chosenProvider = "google";
    }
    if (google) history.replaceState(null, "", "#/settings/calendar");
    draw();
  }

  function setError(error) {
    errorCode = "";
    notice = "";
    draw(failure(error));
  }

  function errorBox(message) {
    return message ? `<div class="notice error" id="caldav-error" role="alert" tabindex="-1">${esc(message)}</div>` : "";
  }

  function draw(message = "") {
    if (!el.isConnected || stale()) return;
    if (errorCode && !message) message = failure({ code: errorCode, message: errorCode });
    errorCode = "";
    el.innerHTML = `<h2 id="caldav-title">${t("iCal / CalDAV calendar")}</h2>
      <p class="sub">${t("Read your personal calendar (iCloud, Google, Fastmail, Nextcloud or any CalDAV server). Your own appointments stay as they are.")}</p>
      <div data-caldav-live role="status" aria-live="polite">${notice ? `<div class="notice info">${esc(notice)}</div>` : ""}</div>
      ${step === "signin" ? signIn(message) : step === "choose" ? choose(message) : step === "confirm" ? confirmStep(message) : connected(message)}
      ${step === "connected" ? classifications() : ""}
      <p class="sub">${t("Copying Garmin workouts onto this calendar is under Sync between sources.")} <a href="#/settings/sync">${t("Sync between sources")}</a></p>`;
    bindStep();
    if (message) el.querySelector("#caldav-error")?.focus();
    else if (focusNext) el.querySelector("[data-step-heading]")?.focus();
    focusNext = true;
    el.querySelectorAll("[data-color]").forEach((s) => s.style.setProperty("background", s.dataset.color));
  }

  // What appointments from each calendar are: only their label on the Calendar page and in the
  // plan the coach reads; none counts as training done. "unclassified" reads the same
  // as an ordinary appointment, so it shows as that choice.
  function classifications() {
    const labels = {
      commitment: t("Ordinary appointments"),
      coaching: t("Sessions with your coach"),
      exercise: t("Your own planned training"),
    };
    const shown = (value) => (value === "coaching" || value === "exercise" ? value : "commitment");
    return `<h3 id="caldav-context-title">${t("Calendar context")}</h3>
      <p class="sub" id="caldav-context-hint">${t("Sets how appointments from each calendar are labelled on the Calendar page and for your coach. None of them counts as training done.")}</p>
      <div class="settings-grid">${status.selected.map((calendar, index) => `<div class="field">
        <label for="caldav-context-${index}">${esc(calendar.name || t("Calendar"))}</label>
        <select class="btn" id="caldav-context-${index}" data-calendar-context="${esc(calendar.url)}" aria-describedby="caldav-context-hint">
          ${Object.entries(labels).map(([value, label]) => `<option value="${value}"${shown(calendar.classification) === value ? " selected" : ""}>${esc(label)}</option>`).join("")}
        </select>
        <p class="sub" data-context-status role="status" aria-live="polite"></p>
      </div>`).join("")}</div>`;
  }

  function providerSelect(current) {
    const names = { icloud: "iCloud", fastmail: "Fastmail", other: t("Nextcloud / other"), google: "Google" };
    return `<label for="caldav-provider">${t("Provider")}</label>
      <select class="btn" id="caldav-provider" name="provider">${status.providers
        .map((p) => `<option value="${esc(p.id)}"${p.id === current ? " selected" : ""}>${esc(names[p.id] || p.name)}</option>`)
        .join("")}</select>`;
  }

  function signIn(message) {
    const current = chosenProvider;
    const described = message ? ' aria-invalid="true" aria-describedby="caldav-error"' : "";
    return `<h3 tabindex="-1" data-step-heading>${t("Step 1 of 2: sign in")}</h3>
      ${errorBox(message)}
      <form data-signin novalidate>
        <div class="settings-grid">
          <div class="field">${providerSelect(current)}</div>
          <div class="field" data-url-field hidden>
            <label for="caldav-url">${t("CalDAV address")}</label>
            <input class="btn" id="caldav-url" name="url" type="url" inputmode="url" autocomplete="url" spellcheck="false" placeholder="https://cloud.example.com/remote.php/dav/" value="${esc(draft.url ?? (status.url && status.provider === "other" ? status.url : ""))}"${described}>
          </div>
        </div>
        <div data-password-fields>
          <div class="settings-grid">
            <div class="field"><label for="caldav-username">${t("Username")}</label>
              <input class="btn" id="caldav-username" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required value="${esc(draft.username ?? (status.username || ""))}"${described}></div>
            <div class="field"><label for="caldav-password">${t("App-specific password")}</label>
              <span class="password-row"><input class="btn" id="caldav-password" name="password" type="password" autocomplete="current-password" required aria-describedby="caldav-hint${message ? " caldav-error" : ""}"${message ? ' aria-invalid="true"' : ""}>
              <button class="btn" type="button" data-toggle-password aria-controls="caldav-password" aria-pressed="false">${t("Show")}</button></span></div>
          </div>
          <p class="sub" id="caldav-hint" data-hint></p>
          <div class="settings-save"><button class="btn primary" type="submit">${t("Connect")}</button>
            ${status.connected ? `<button class="btn" type="button" data-cancel>${t("Cancel")}</button>` : ""}</div>
          <p class="sub">${t("Stored encrypted on this server, never shown again. VibeSynergy checks the sign-in and lists your calendars before saving anything else.")}</p>
        </div>
        <div data-google-fields hidden>${googleFields(message)}</div>
      </form>`;
  }

  function googleFields(message) {
    const g = status.google;
    const described = message ? ' aria-describedby="caldav-error" aria-invalid="true"' : "";
    return `<p class="sub">${t("Google needs your own OAuth client (Google Cloud console → Credentials → OAuth client ID, type Web application). Enter it once; it is stored encrypted. Add this address as an authorized redirect URI:")}</p>
      <div class="field"><label for="caldav-redirect">${t("Redirect URI")}</label>
        <input class="btn" id="caldav-redirect" readonly value="${esc(g.redirect_uri)}"></div>
      <div class="settings-grid">
        <div class="field"><label for="caldav-gid">${t("Google client ID")}${g.client_set ? ` ${t("(set)")}` : ""}</label>
          <input class="btn" id="caldav-gid" name="client_id" autocomplete="off" spellcheck="false"${described}></div>
        <div class="field"><label for="caldav-gsecret">${t("Google client secret")}${g.client_set ? ` ${t("(set)")}` : ""}</label>
          <input class="btn" id="caldav-gsecret" name="client_secret" type="password" autocomplete="off"${described}></div>
      </div>
      <div class="settings-save">
        <button class="btn" type="button" data-google-save>${t("Save Google client")}</button>
        <button class="btn primary" type="button" data-google-start${g.client_set ? "" : " disabled"}>${t("Sign in with Google")}</button>
        ${status.connected ? `<button class="btn" type="button" data-cancel>${t("Cancel")}</button>` : ""}</div>
      <p class="sub">${t("VibeSynergy asks Google only for read access to your calendars.")}</p>`;
  }

  function swatch(color) {
    return color
      ? `<span class="swatch" data-color="${esc(color)}" aria-hidden="true"></span><span class="sub">${t("Colour")} ${esc(color)}</span>`
      : `<span class="sub">${t("No colour")}</span>`;
  }

  function choose(message) {
    const rows = status.discovered.length
      ? status.discovered
          .map((c, i) => `<li class="cal-option"><input type="checkbox" id="cal-${i}" value="${esc(c.url)}"${c.selected ? " checked" : ""} aria-describedby="cal-${i}-meta">
            <label for="cal-${i}"><span class="cal-name">${esc(c.name)}</span></label>
            <span class="cal-meta" id="cal-${i}-meta">${swatch(c.color)} <span class="sub cal-url">${esc(c.url)}</span></span></li>`)
          .join("")
      : `<li class="sub">${t("No calendars were found for this account.")}</li>`;
    return `<h3 tabindex="-1" data-step-heading>${t("Step 2 of 2: choose calendars")}</h3>
      ${errorBox(message)}
      <p class="sub">${t("Signed in as {name} ({provider}).", { name: status.username || providerName(), provider: providerName() })}</p>
      <form data-choose novalidate>
        <fieldset class="cal-list"${message ? ' aria-describedby="caldav-error"' : ""}><legend>${t("Calendars to read")}</legend>
          <ul>${rows}</ul></fieldset>
        <div class="settings-save"><button class="btn primary" type="submit"${status.discovered.length ? "" : " disabled"}>${t("Save selection")}</button>
          ${status.selected.length ? `<button class="btn" type="button" data-cancel>${t("Cancel")}</button>` : ""}
          <button class="btn" type="button" data-refresh>${t("Look again")}</button></div>
      </form>`;
  }

  function providerName() {
    const p = status.providers.find((x) => x.id === status.provider);
    return status.provider === "other" ? t("Nextcloud / other") : p?.name || "";
  }

  function syncLine() {
    const s = status.sync;
    if (!s) return t("Not synced yet. The first sync runs after the service starts it.");
    if (s.problem) return syncAdvice("caldav", s.problem);
    return t("Last synced {when}.", { when: ago(s.last_success_at) });
  }

  function connected(message) {
    return `<h3 tabindex="-1" data-step-heading>${t("Connected")}</h3>
      ${errorBox(message)}
      <dl class="settings-system">
        <dt>${t("Provider")}</dt><dd>${esc(providerName())}</dd>
        ${status.username ? `<dt>${t("Username")}</dt><dd>${esc(status.username)}</dd>` : ""}
        <dt>${t("Password")}</dt><dd>${status.password_set || status.kind === "oauth" ? t("saved (hidden)") : t("(not set)")}</dd>
        <dt>${t("Calendars")}</dt><dd><ul class="cal-selected">${status.selected
          .map((c) => `<li>${esc(c.name)} <span class="sub">${swatch(c.color)}</span></li>`)
          .join("")}</ul></dd>
        <dt>${t("Sync")}</dt><dd>${esc(syncLine())}${status.sync?.problem ? ` <button type="button" class="btn" data-fix-sync>${t("Fix")}</button>` : ""}${syncDetails(status.sync?.problem)}</dd>
      </dl>
      <div class="settings-save">
        <button class="btn" type="button" data-change>${t("Change calendars")}</button>
        <button class="btn" type="button" data-signin-again>${t("Sign in again")}</button>
        <button class="btn" type="button" data-disconnect>${t("Disconnect")}</button></div>`;
  }

  function confirmStep(message) {
    return `<h3 tabindex="-1" data-step-heading>${t("Disconnect this calendar account?")}</h3>
      ${errorBox(message)}
      <p>${t("This deletes the stored password or tokens and the chosen calendars. Calendar items already archived stay in your history. Your calendar itself is not changed.")}</p>
      <div class="settings-save"><button class="btn primary" type="button" data-confirm-disconnect>${t("Yes, disconnect")}</button>
        <button class="btn" type="button" data-cancel>${t("Cancel")}</button></div>`;
  }

  function show(next, message = "") {
    step = next;
    notice = message;
    errorCode = "";
    draw();
  }

  function bindStep() {
    el.querySelectorAll("[data-calendar-context]").forEach((select) => {
      select.addEventListener("change", async () => {
        const calendar = status.selected.find((c) => c.url === select.dataset.calendarContext);
        const prior = ["coaching", "exercise"].includes(calendar.classification) ? calendar.classification : "commitment";
        const message = select.parentElement.querySelector("[data-context-status]");
        select.disabled = true;
        try {
          status = (await post("caldav/classification", {
            url: calendar.url, classification: select.value,
          })).data;
          message.textContent = t("Saved.");
        } catch (error) {
          select.value = prior;
          message.textContent = failure(error);
        } finally {
          select.disabled = false;
          select.focus();
        }
      });
    });

    const form = el.querySelector("[data-signin]");
    if (form) bindSignIn(form);
    el.querySelector("[data-choose]")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const urls = [...el.querySelectorAll(".cal-list input:checked")].map((i) => i.value);
      if (!urls.length) return setError({ code: "no_selection" });
      try {
        status = (await post("caldav/calendars", { urls })).data;
        show("connected", t("Saved. Calendars selected: {n}.", { n: status.selected.length }));
      } catch (error) {
        setError(error);
      }
    });
    el.querySelectorAll("[data-cancel]").forEach((b) => b.addEventListener("click", () => show(status.connected && status.selected.length ? "connected" : status.connected ? "choose" : "signin")));
    el.querySelector("[data-change]")?.addEventListener("click", async () => {
      try {
        status = (await post("caldav/discover", {})).data;
        show("choose");
      } catch (error) {
        setError(error);
      }
    });
    el.querySelector("[data-refresh]")?.addEventListener("click", async () => {
      try {
        status = (await post("caldav/discover", {})).data;
        notice = t("The list is up to date.");
        focusNext = false;
        draw();
      } catch (error) {
        setError(error);
      }
    });
    el.querySelector("[data-fix-sync]")?.addEventListener("click", () => show("signin"));
    el.querySelector("[data-signin-again]")?.addEventListener("click", () => show("signin"));
    el.querySelector("[data-disconnect]")?.addEventListener("click", () => show("confirm"));
    el.querySelector("[data-confirm-disconnect]")?.addEventListener("click", async () => {
      try {
        status = (await post("caldav/disconnect", {})).data;
        chosenProvider = "icloud";
        show("signin", t("Disconnected. The stored credentials were deleted."));
      } catch (error) {
        setError(error);
      }
    });
  }

  function bindSignIn(form) {
    const provider = form.querySelector("#caldav-provider");
    const google = form.querySelector("[data-google-fields]");
    const passwordFields = form.querySelector("[data-password-fields]");
    const urlField = form.querySelector("[data-url-field]");
    const hint = form.querySelector("[data-hint]");
    function sync() {
      const id = provider.value;
      const isGoogle = id === "google";
      google.hidden = !isGoogle;
      passwordFields.hidden = isGoogle;
      urlField.hidden = id !== "other";
      hint.textContent = t(PROVIDER_HINTS[id] || PROVIDER_HINTS.other);
    }
    provider.addEventListener("change", () => {
      chosenProvider = provider.value;
      sync();
    });
    sync();
    form.querySelector("[data-toggle-password]")?.addEventListener("click", (event) => {
      const input = form.querySelector("#caldav-password");
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      event.target.textContent = show ? t("Hide") : t("Show");
      event.target.setAttribute("aria-pressed", String(show));
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(form));
      draft = { username: data.username || "", url: data.url || "" };
      const submit = form.querySelector("[type=submit]");
      submit.disabled = true;
      submit.textContent = t("Connecting…");
      try {
        status = (await post("caldav/connect", {
          provider: data.provider,
          url: data.url || null,
          username: data.username || "",
          password: data.password || "",
        })).data;
        draft = {};
        show("choose");
      } catch (error) {
        setError(error);
      }
    });
    form.querySelector("[data-google-save]")?.addEventListener("click", async () => {
      try {
        status = (await post("caldav/google/app", {
          client_id: form.querySelector("[name=client_id]").value,
          client_secret: form.querySelector("[name=client_secret]").value,
        })).data;
        notice = t("Google client saved.");
        focusNext = false;
        draw();
      } catch (error) {
        setError(error);
      }
    });
    form.querySelector("[data-google-start]")?.addEventListener("click", async () => {
      try {
        const reply = await post("caldav/google/start", {});
        location.href = reply.data.authorization_url;
      } catch (error) {
        setError(error);
      }
    });
    form.querySelector("[data-cancel]")?.addEventListener("click", () => show(status.selected.length ? "connected" : "choose"));
  }

  el.innerHTML = `<h2>${t("iCal / CalDAV calendar")}</h2><p class="sub">${t("Loading…")}</p>`;
  await load();
}
