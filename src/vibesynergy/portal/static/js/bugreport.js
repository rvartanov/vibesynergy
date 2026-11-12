// Report a bug: the button (in the footer, and under Settings → System) opens GitHub's bug report form in a new tab, in your own GitHub
// account, with what the page knows filled in (version, page, the error it shows, the API
// calls that failed lately, browser). You add what happened and send it there; nothing is
// sent from this server. What a report needs: docs/REPORTING.md.
import { failures, get } from "./api.js";

const MAX_URL = 7000; // GitHub refuses very long addresses; the details are cut to fit

let target = "https://github.com/rvartanov/vibesynergy/issues/new";
let version = "";

let prepared = false;

// Ready before the click: opening the tab must happen in the click itself (popup blockers).
function prepare() {
  if (prepared) return;
  prepared = true;
  get("bugs")
    .then((reply) => {
      target = reply.data.new_issue_url || target;
    })
    .catch(() => {});
  fetch("/version", { credentials: "same-origin" })
    .then((reply) => reply.json())
    .then((info) => {
      version = [info.version, info.commit].filter(Boolean).join(" · ");
    })
    .catch(() => {});
}

export function bindBugReport(button) {
  if (!button) return;
  prepare();
  button.addEventListener("click", () => window.open(issueUrl(), "_blank", "noopener"));
}

function shownErrors() {
  return [...document.querySelectorAll("#view .notice.error, #view .error-text")]
    .map((el) => el.textContent.trim())
    .filter(Boolean)
    .slice(0, 5);
}

// Only the route, not what follows "?" (filters, ids of shared links): the issue is public.
function page() {
  return (location.hash || "#/").split("?")[0];
}

// What the page saw, for the form's "Errors the page showed" field.
export function errorsText(errors, failed) {
  const lines = [...errors];
  for (const r of failed) lines.push(`${r.method} ${String(r.url).split("?")[0]} → ${r.status}`);
  return lines.join("\n");
}

function issueUrl() {
  let errors = shownErrors().map((e) => e.slice(0, 500));
  const title = errors[0]?.slice(0, 70) || "";
  let failed = failures().slice(0, 10);
  const build = () => {
    // The bug report form (.github/ISSUE_TEMPLATE/bug_report.yml); its fields by their id.
    const params = new URLSearchParams({
      template: "bug_report.yml",
      title: title ? `Bug: ${title}` : "Bug: ",
      version,
      system: navigator.userAgent,
      page: page(),
    });
    const seen = errorsText(errors, failed);
    if (seen) params.set("errors", seen);
    return `${target}?${params}`;
  };
  let url = build();
  // Too long for GitHub: fewer failed requests first, then fewer errors.
  while (url.length > MAX_URL && (failed.length || errors.length)) {
    if (failed.length) failed = failed.slice(0, -1);
    else errors = errors.slice(0, -1);
    url = build();
  }
  return url;
}
