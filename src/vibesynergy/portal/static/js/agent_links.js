// Settings → Sync between sources: links that tell a AI coach about a new activity.
// Secrets are write-only: the page sends one and only ever sees has_secret. Each field saves
// on change; the server validates and its English message is translated here.
import { get, post } from "./api.js";
import { ago, esc, userDateTime } from "./format.js";
import { N, t } from "./i18n.js";

const KINDS = [
  ["claude_routine", N("Claude Code routine")],
  ["webhook", N("Webhook with signature")],
  ["bearer", N("Webhook with token")],
];
const STATES = {
  pending: N("Waiting"),
  retry: N("Trying again soon"),
  sending: N("Sending"),
  sent: N("Sent"),
  failed: N("Not accepted"),
  gave_up: N("Stopped trying"),
  skipped: N("Not needed"),
  expired: N("Too late, not sent"),
  unknown: N("Interrupted, not sent again"),
};
// What a link listens for; the public Strava activity is the coach insight's trigger.
const EVENTS = [
  ["activity.published", N("You make the activity public on Strava")],
  ["activity.ready", N("A new activity is in, right after the workout")],
  ["coach.run_missed", N("A coach routine did not run (morning, evening, weekly)")],
];
const WHAT = {
  "vibesynergy.test": N("Test"),
  "activity.published": N("Made public"),
  "activity.ready": N("New activity"),
  "coach.run_missed": N("Missed coach run"),
};
const GOOD = new Set(["sent", "skipped"]);
const WAIT = new Set(["pending", "retry", "sending"]);

function kindOptions(selected) {
  return KINDS.map(([value, label]) => `<option value="${value}"${value === selected ? " selected" : ""}>${esc(t(label))}</option>`).join("");
}

function addressHelp(kind) {
  return kind === "claude_routine"
    ? t("The routine's API address, or just its id (trig_…).")
    : t("An https:// address of your AI coach.");
}

function secretHelp(kind) {
  if (kind === "claude_routine") return t("The routine's API token. VibeSynergy keeps it encrypted and never shows it again.");
  if (kind === "webhook") return t("VibeSynergy signs every message with this secret (Standard Webhooks). Give the same secret to your AI coach.");
  return t("VibeSynergy sends this token with every message. It is kept encrypted and never shown again.");
}

function randomSecret() {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return `whsec_${btoa(String.fromCharCode(...bytes))}`;
}

function lastLine(sub) {
  const last = (sub.deliveries || []).find((d) => d.state !== "pending") || sub.deliveries?.[0];
  if (!last) return t("Nothing sent yet.");
  const when = last.sent_at || last.attempted_at || last.created_at;
  if (last.state === "sent") return t("Last sent {when}.", { when: ago(when) });
  if (WAIT.has(last.state)) return t("A message is waiting to be sent.");
  if (last.state === "skipped") return t("Last message was not needed: the coach insight was already there.");
  return t("Last message: {state}.", { state: t(STATES[last.state] || last.state) });
}

function logRows(sub) {
  const rows = sub.deliveries || [];
  if (!rows.length) return `<p class="sub">${esc(t("Nothing sent yet."))}</p>`;
  return `<ul class="trends">${rows.map((d) => {
    const when = d.sent_at || d.attempted_at || d.created_at;
    const what = t(WHAT[d.type] || WHAT["activity.ready"]);
    const status = d.http_status ? ` · HTTP ${esc(d.http_status)}` : "";
    const session = d.link ? ` · <a href="${esc(d.link)}" target="_blank" rel="noopener noreferrer">${esc(t("Open session"))}</a>` : "";
    const tone = GOOD.has(d.state) ? "good" : WAIT.has(d.state) ? "" : "warning";
    return `<li><span class="chip ${tone}">${esc(t(STATES[d.state] || d.state))}</span> ${esc(what)} · ${esc(userDateTime(when))}${status}${session}${d.error && !GOOD.has(d.state) ? ` <span class="sub">${esc(d.error)}</span>` : ""}</li>`;
  }).join("")}</ul>`;
}

function linkHtml(sub) {
  const id = esc(sub.id);
  return `<li class="agent-link" data-agent-link="${id}">
    <h3 class="settings-subhead">${esc(sub.name)}</h3>
    <label class="check"><input type="checkbox" name="enabled" aria-describedby="${id}-last"${sub.enabled ? " checked" : ""}> ${esc(t("Send new activities to this link"))}</label>
    <p class="sub" id="${id}-last">${esc(lastLine(sub))}</p>
    <details><summary>${esc(t("Link settings"))}</summary>
      <div class="settings-grid">
        <label>${esc(t("Name"))} <input class="btn" name="name" maxlength="60" autocomplete="off" value="${esc(sub.name)}"></label>
        <label>${esc(t("Kind"))} <select class="btn" name="auth">${kindOptions(sub.auth)}</select></label>
        <label>${esc(t("Address"))} <input class="btn" name="url" autocomplete="off" spellcheck="false" aria-describedby="${id}-url-help" value="${esc(sub.url)}"></label>
        <label>${esc(sub.auth === "webhook" ? t("Signing secret") : t("Token"))} <input class="btn" name="secret" type="password" autocomplete="new-password" spellcheck="false" aria-describedby="${id}-secret-help" placeholder="${esc(sub.has_secret ? t("Saved. Type a new one to replace it.") : t("Not set"))}"></label>
      </div>
      <p class="sub" id="${id}-url-help">${esc(addressHelp(sub.auth))}</p>
      <p class="sub" id="${id}-secret-help">${esc(secretHelp(sub.auth))}</p>
      <fieldset class="settings-datasets"><legend>${esc(t("Send a message when"))}</legend>
        ${EVENTS.map(([value, label]) => `<label class="check"><input type="checkbox" name="event_type" value="${value}"${(sub.events || []).includes(value) ? " checked" : ""}> ${esc(t(label))}</label>`).join("")}
      </fieldset>
      <label class="check"><input type="checkbox" name="only_missing_insight"${sub.only_missing_insight ? " checked" : ""}> ${esc(t("Only activities without a coach insight"))}</label>
      <div class="settings-grid">
        <label>${esc(t("At most per day"))} <input class="btn" name="daily_cap" type="number" min="1" max="50" step="1" inputmode="numeric" value="${esc(sub.daily_cap)}"></label>
        <label>${esc(t("Quiet from"))} <input class="btn" name="quiet_start" type="time" value="${esc(sub.quiet_start)}"></label>
        <label>${esc(t("Quiet until"))} <input class="btn" name="quiet_end" type="time" value="${esc(sub.quiet_end)}"></label>
      </div>
      <p class="sub">${esc(t("During quiet hours messages wait until the morning."))}</p>
    </details>
    <details><summary>${esc(t("Recent messages"))}</summary>${logRows(sub)}</details>
    <div class="settings-save">
      <button class="btn" type="button" data-agent-test${sub.has_secret ? "" : " disabled"}>${esc(t("Send a test"))}</button>
      <button class="btn" type="button" data-agent-remove>${esc(t("Remove link"))}</button>
    </div>
    <p class="sub" role="status" aria-live="polite" data-agent-status></p>
  </li>`;
}

function addHtml(data) {
  const full = data.subscriptions.length >= data.defaults.max;
  if (full) return "";
  return `<details data-agent-add><summary>${esc(t("Add a link"))}</summary>
    <div class="settings-grid">
      <label>${esc(t("Name"))} <input class="btn" name="name" maxlength="60" autocomplete="off" placeholder="${esc(t("Coach after a workout"))}"></label>
      <label>${esc(t("Kind"))} <select class="btn" name="auth">${kindOptions("claude_routine")}</select></label>
      <label>${esc(t("Address"))} <input class="btn" name="url" autocomplete="off" spellcheck="false" aria-describedby="agent-add-url-help"></label>
      <label><span data-agent-secret-label>${esc(t("Token"))}</span> <input class="btn" name="secret" type="password" autocomplete="new-password" spellcheck="false" aria-describedby="agent-add-secret-help"></label>
    </div>
    <p class="sub" id="agent-add-url-help">${esc(addressHelp("claude_routine"))}</p>
    <p class="sub" id="agent-add-secret-help">${esc(secretHelp("claude_routine"))}</p>
    <div class="settings-save">
      <button class="btn" type="button" data-agent-generate hidden>${esc(t("Make a secret"))}</button>
      <button class="btn primary" type="button" data-agent-create>${esc(t("Add link"))}</button>
    </div>
    <p class="sub" role="status" aria-live="polite" data-agent-add-status></p>
  </details>`;
}

function render(el, data) {
  el.innerHTML = `${data.subscriptions.length ? `<ul class="agent-links">${data.subscriptions.map(linkHtml).join("")}</ul>` : ""}${addHtml(data)}`;
}

function errorText(error) {
  return t("Not saved: {error}", { error: t(error.message) });
}

export async function agentLinkSettings(el, { stale } = {}) {
  let data = (await get("events/subscriptions")).data;
  if (stale?.() || !el.isConnected) return;
  render(el, data);

  const redraw = (fresh, focusSelector) => {
    data = fresh;
    render(el, data);
    if (focusSelector) el.querySelector(focusSelector)?.focus();
  };

  el.addEventListener("change", async (event) => {
    const field = event.target;
    const item = field.closest("[data-agent-link]");
    if (item) {
      const status = item.querySelector("[data-agent-status]");
      const name = field.name;
      const value = field.type === "checkbox" ? field.checked : field.value;
      let body = { [name]: value };
      if (name === "daily_cap") body = { daily_cap: Number(value) };
      if (name === "event_type") {
        body = { events: [...item.querySelectorAll('[name="event_type"]:checked')].map((box) => box.value) };
      }
      if (name === "secret" && !value) return;
      const inSettings = Boolean(field.closest("details"));
      field.disabled = true;
      try {
        const fresh = (await post(`events/subscriptions/${item.dataset.agentLink}`, body)).data;
        const id = item.dataset.agentLink;
        redraw(fresh, null);
        // Keep the open settings open, and the focus on the field just changed.
        if (inSettings) el.querySelector(`[data-agent-link="${id}"] details`).open = true;
        const target = name === "event_type" ? `[name="event_type"][value="${field.value}"]` : `[name="${name}"]`;
        el.querySelector(`[data-agent-link="${id}"] ${target}`)?.focus();
        el.querySelector(`[data-agent-link="${id}"] [data-agent-status]`).textContent = t("Saved.");
      } catch (error) {
        if (field.type === "checkbox") field.checked = !field.checked;
        status.textContent = errorText(error);
      } finally {
        field.disabled = false;
      }
      return;
    }
    if (field.name === "auth" && field.closest("[data-agent-add]")) {
      const add = field.closest("[data-agent-add]");
      add.querySelector("#agent-add-url-help").textContent = addressHelp(field.value);
      add.querySelector("#agent-add-secret-help").textContent = secretHelp(field.value);
      add.querySelector("[data-agent-secret-label]").textContent = field.value === "webhook" ? t("Signing secret") : t("Token");
      add.querySelector("[data-agent-generate]").hidden = field.value !== "webhook";
    }
  });

  el.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const item = button.closest("[data-agent-link]");
    if (button.matches("[data-agent-generate]")) {
      const input = button.closest("[data-agent-add]").querySelector('[name="secret"]');
      input.type = "text";  // shown once, here only, so it can be copied to the AI coach
      input.value = randomSecret();
      input.select();
      button.closest("[data-agent-add]").querySelector("[data-agent-add-status]").textContent = t("Copy this secret to your AI coach now; VibeSynergy won't show it again.");
      return;
    }
    if (button.matches("[data-agent-create]")) {
      const add = button.closest("[data-agent-add]");
      const status = add.querySelector("[data-agent-add-status]");
      const body = Object.fromEntries(["name", "auth", "url", "secret"].map((key) => [key, add.querySelector(`[name="${key}"]`).value.trim()]));
      if (!body.name) body.name = t("Coach after a workout");
      button.disabled = true;
      try {
        const fresh = (await post("events/subscriptions", body)).data;
        const created = fresh.subscriptions[fresh.subscriptions.length - 1];
        redraw(fresh, created ? `[data-agent-link="${created.id}"] [name="enabled"]` : null);
        const freshStatus = created && el.querySelector(`[data-agent-link="${created.id}"] [data-agent-status]`);
        if (freshStatus) freshStatus.textContent = t("Link added. Switch it on when you are ready.");
      } catch (error) {
        status.textContent = errorText(error);
        button.disabled = false;
      }
      return;
    }
    if (!item) return;
    const status = item.querySelector("[data-agent-status]");
    const id = item.dataset.agentLink;
    if (button.matches("[data-agent-test]")) {
      button.disabled = true;
      status.textContent = t("Sending a test…");
      try {
        const fresh = (await post(`events/subscriptions/${id}/test`)).data;
        const delivery = fresh.delivery;
        redraw(fresh, `[data-agent-link="${id}"] [data-agent-test]`);
        const line = el.querySelector(`[data-agent-link="${id}"] [data-agent-status]`);
        if (delivery.state === "sent") {
          line.innerHTML = `${esc(t("Test sent."))}${delivery.link ? ` <a href="${esc(delivery.link)}" target="_blank" rel="noopener noreferrer">${esc(t("Open session"))}</a>` : ""}`;
        } else {
          line.textContent = t("The test was not accepted: {error}", { error: delivery.error || t(STATES[delivery.state] || delivery.state) });
        }
      } catch (error) {
        status.textContent = t("The test was not sent: {error}", { error: t(error.message) });
        button.disabled = false;
      }
      return;
    }
    if (button.matches("[data-agent-remove]")) {
      if (!window.confirm(t("Remove this link? Your AI coach stops getting messages from VibeSynergy."))) return;
      try {
        redraw((await post(`events/subscriptions/${id}/delete`)).data, "[data-agent-add] summary");
      } catch (error) {
        status.textContent = errorText(error);
      }
    }
  });
}
