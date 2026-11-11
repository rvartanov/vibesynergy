// Settings → System → Bug reports: the GitHub account that files reports from the bug button
// as issues (connected with a code on github.com), or a token instead (write-only), and the
// reports kept on this server, with a way to send unsent ones.
import { get, post } from "./api.js";
import { ago, esc } from "./format.js";
import { t } from "./i18n.js";

// Connect a GitHub account: a short code to enter on github.com, then this page waits for it.
export async function connectGithub(host, onDone) {
  const say = (html) => {
    host.innerHTML = html;
  };
  let start;
  try {
    start = (await post("bugs/connect")).data;
  } catch (error) {
    say(`<p class="notice error" role="alert">${esc(error.message)}</p>`);
    return;
  }
  say(`<p>${esc(t("Enter this code on GitHub, then come back here:"))}</p>
    <p class="bug-code"><code>${esc(start.user_code)}</code></p>
    <p><a class="btn" href="${esc(start.verification_uri)}" target="_blank" rel="noopener">${t("Open GitHub")}</a></p>
    <p class="sub" role="status" aria-live="polite">${t("Waiting for GitHub…")}</p>`);
  let wait = start.interval * 1000;
  const deadline = Date.now() + start.expires_in * 1000;
  const poll = async () => {
    if (!host.isConnected) return;
    let r;
    try {
      r = (await post("bugs/connect/poll")).data;
    } catch (error) {
      say(`<p class="notice error" role="alert">${esc(error.message)}</p>`);
      return;
    }
    if (r.status === "done") return onDone(r.login);
    if (r.status === "slow_down") wait += 5000;
    if ((r.status === "pending" || r.status === "slow_down") && Date.now() < deadline) {
      setTimeout(poll, wait);
      return;
    }
    say(`<p class="notice error" role="alert">${esc(r.status === "denied" ? t("GitHub was not allowed. Nothing changed.") : t("The code expired. Try again."))}</p>`);
  };
  setTimeout(poll, wait);
}

export async function mountBugSettings(el, flash = "") {
  if (!el) return;
  let d;
  try {
    d = (await get("bugs")).data;
  } catch (error) {
    el.innerHTML = `<div class="notice error">${esc(error.message)}</div>`;
    return;
  }
  if (!el.isConnected) return;
  const rows = d.reports
    .slice(0, 10)
    .map(
      (r) => `<tr><td>${esc(r.description.split("\n")[0].slice(0, 80))}</td><td>${esc(ago(r.created))}</td>
        <td>${r.issue_url ? `<a href="${esc(r.issue_url)}" target="_blank" rel="noopener">${t("issue")}</a>` : `<span class="muted">${esc(r.send_error || t("not sent"))}</span> <button class="btn small" type="button" data-send-bug="${esc(r.id)}">${t("Send")}</button>`}</td></tr>`,
    )
    .join("");
  const who = d.connection.kind === "account"
    ? `<p>${esc(t("Reports are filed as {login} on GitHub.", { login: `@${d.connection.login}` }))}</p>
       <div class="settings-save"><button class="btn" type="button" data-bug-disconnect>${t("Disconnect GitHub")}</button></div>`
    : d.can_connect
      ? `<div data-bug-connect><button class="btn primary" type="button" data-bug-connect-start>${t("Connect GitHub")}</button></div>`
      : "";
  el.innerHTML = `<p class="sub">${esc(t("The bug button at the top files a report as an issue on {repo} on GitHub. Without a connection, reports wait here.", { repo: d.repo }))}</p>
    ${flash ? `<div class="notice" role="status">${esc(flash)}</div>` : ""}
    ${who}
    <details class="sub"${d.can_connect ? "" : " open"}><summary>${t("Use a token instead")}</summary>
      <p>${esc(t("On GitHub: Settings → Developer settings → Fine-grained tokens → Generate. Repository access: only {repo}. Permissions: Issues, read and write. Nothing else.", { repo: d.repo }))}</p>
    <div class="settings-grid">
      <label for="bug-token">${t("GitHub token")} ${d.token_set ? t("(set)") : t("(not set)")}</label>
      <input class="btn" id="bug-token" type="password" autocomplete="off" spellcheck="false" placeholder="${d.token_set ? esc(t("enter to replace")) : "github_pat_…"}">
    </div>
    <div class="settings-save"><button class="btn" type="button" data-bug-token>${t("Save token")}</button><span class="sub" role="status" aria-live="polite" data-bug-status></span></div>
    </details>
    ${rows ? `<div class="table-wrap"><table><thead><tr><th scope="col">${t("Report")}</th><th scope="col">${t("When")}</th><th scope="col">GitHub</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="empty">${t("No reports yet.")}</p>`}`;
  el.querySelector("[data-bug-connect-start]")?.addEventListener("click", () =>
    connectGithub(el.querySelector("[data-bug-connect]"), (login) => mountBugSettings(el, t("Connected as {login}.", { login: `@${login}` }))),
  );
  el.querySelector("[data-bug-disconnect]")?.addEventListener("click", async () => {
    await post("bugs/disconnect");
    mountBugSettings(el, t("GitHub is disconnected on this server."));
  });
  el.querySelector("[data-bug-token]").addEventListener("click", async () => {
    try {
      await post("bugs/token", { value: el.querySelector("#bug-token").value });
      mountBugSettings(el, t("Token saved on this server."));
    } catch (error) {
      el.querySelector("[data-bug-status]").textContent = error.message;
    }
  });
  for (const button of el.querySelectorAll("[data-send-bug]")) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const r = (await post(`bugs/${button.dataset.sendBug}/send`)).data;
        mountBugSettings(el, r.issue_url ? t("Sent.") : t("Not sent: {error}", { error: r.send_error }));
      } catch (error) {
        button.disabled = false;
        el.querySelector("[data-bug-status]").textContent = error.message;
      }
    });
  }
}
