// Report a bug: a dialog that collects what the page knows (address, the error it shows,
// the API calls that failed lately, version, browser) with your description and an optional
// screenshot. Kept on this server and filed as a GitHub issue as your connected GitHub account
// (or with the install's token).
import { failures, get, meta, post } from "./api.js";
import { connectGithub } from "./bug_settings.js";
import { esc } from "./format.js";
import { t } from "./i18n.js";

export function bindBugReport(button) {
  button?.addEventListener("click", open);
}

function shownErrors() {
  return [...document.querySelectorAll("#view .notice.error, #view .error-text")]
    .map((el) => el.textContent.trim())
    .filter(Boolean)
    .slice(0, 5);
}

async function open() {
  let version = "";
  try {
    version = (await meta()).data.version;
  } catch {
    // the report says so without it
  }
  let who = null;
  try {
    who = (await get("bugs")).data;
  } catch {
    // the report is still kept here
  }
  const context = {
    page: location.hash || "#/",
    errors: shownErrors(),
    failed_requests: failures(),
    browser: navigator.userAgent,
  };
  const dialog = document.createElement("dialog");
  dialog.className = "bug-dialog card";
  dialog.innerHTML = `
    <form method="dialog">
      <h2>${t("Report a bug")}</h2>
      <label>${t("What happened, and what did you expect?")}
        <textarea class="btn" name="description" rows="5" required></textarea></label>
      <label>${t("Screenshot (optional)")} <input type="file" name="screenshot" accept="image/png,image/jpeg,image/webp"></label>
      <details><summary>${t("Sent along")}</summary>
        <ul class="sub">
          <li>${t("Page")}: <code>${esc(context.page)}</code>${version ? ` · VibeSynergy ${esc(version)}` : ""}</li>
          <li>${t("Shown on the page")}: ${context.errors.length ? context.errors.map(esc).join("; ") : t("no error")}</li>
          <li>${t("Failed requests")}: ${context.failed_requests.length ? context.failed_requests.slice(0, 5).map((r) => `${esc(r.method)} ${esc(r.url)} → ${esc(r.status)}`).join("; ") : t("none")}</li>
          <li>${t("Browser")}: ${esc(context.browser)}</li>
        </ul>
      </details>
      <div class="sub" data-bug-who>${whoHtml(who)}</div>
      <div class="settings-save"><button class="btn primary" type="submit" value="send">${t("Send")}</button><button class="btn" type="button" data-close>${t("Cancel")}</button><span class="sub" role="status" aria-live="polite" data-status></span></div>
    </form>`;
  document.body.append(dialog);
  dialog.addEventListener("close", () => dialog.remove());
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  const form = dialog.querySelector("form");
  const whoBox = dialog.querySelector("[data-bug-who]");
  whoBox.querySelector("[data-bug-connect-start]")?.addEventListener("click", () =>
    connectGithub(whoBox, (login) => {
      whoBox.textContent = t("Sent as {login} on GitHub.", { login: `@${login}` });
    }),
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = dialog.querySelector("[data-status]");
    const button = form.querySelector("[type=submit]");
    button.disabled = true;
    status.textContent = t("Sending…");
    try {
      const file = form.screenshot.files[0];
      const screenshot = file ? await dataUrl(file) : null;
      const reply = await post("bugs", { ...context, description: form.description.value, screenshot });
      const r = reply.data;
      form.innerHTML = `<h2>${t("Thank you")}</h2>
        <p>${r.issue_url ? `${t("Filed as")} <a href="${esc(r.issue_url)}" target="_blank" rel="noopener">${esc(r.issue_url.replace(/^https:\/\/github.com\//, ""))}</a>.` : esc(t("Kept on this server ({reason}). It can be sent from Settings → System.", { reason: r.send_error || t("not sent") }))}</p>
        <div class="settings-save"><button class="btn" type="button" data-close>${t("Close")}</button></div>`;
      form.querySelector("[data-close]").addEventListener("click", () => dialog.close());
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
  dialog.showModal();
  form.description.focus();
}

function whoHtml(who) {
  if (who?.connection?.kind === "account") return esc(t("Sent as {login} on GitHub.", { login: `@${who.connection.login}` }));
  if (who?.connection?.kind === "token") return esc(t("Sent to GitHub with this server's token."));
  if (who?.can_connect) return `${esc(t("Connect GitHub to send it as you; otherwise it waits on this server."))} <button class="btn small" type="button" data-bug-connect-start>${t("Connect GitHub")}</button>`;
  return esc(t("Kept on this server until GitHub is set up (Settings → System)."));
}

function dataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
