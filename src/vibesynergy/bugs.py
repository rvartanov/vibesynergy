"""Bug reports from the portal: kept on this server, and filed as GitHub issues on the
project's repository (``[bugs] repo``). Issues are filed as the GitHub account you connect
(GitHub's device flow through the project's GitHub App, ``[bugs] github_client_id``), or
with a token set up by hand (Settings -> System). Without either, or when GitHub can't be
reached, a report stays here and can be sent later.

A report carries what the page knew: its address, the error it showed, the API calls that
failed lately, the version and the browser, plus the description. A screenshot, if added,
stays on this server (the issue says where): GitHub's API takes no attachments.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from vibesynergy import __version__
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now

REPO = "rvartanov/vibesynergy"  # the default; ``[bugs] repo`` points elsewhere
SECRETS = "github"
TOKEN = "token"
MAX_SCREENSHOT = 8 * 1024 * 1024
API = "https://api.github.com"
WEB = "https://github.com"
DEVICE = "device"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_ID = re.compile(r"bug_[0-9a-f]{12}")


def repo(settings: Any) -> str:
    value = str((getattr(settings, "bugs", None) or {}).get("repo") or REPO)
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_-][A-Za-z0-9_.-]*", value):
        raise ValueError("[bugs] repo must look like owner/name")
    return value


def folder(settings: Any) -> Path:
    return settings.data_dir / "bug-reports"


def _store(settings: Any) -> SecretStore:
    return SecretStore(settings.secrets_dir, SECRETS)


def client_id(settings: Any) -> str | None:
    """The GitHub App that files issues as your account (only its public client id)."""
    value = str((getattr(settings, "bugs", None) or {}).get("github_client_id") or "").strip()
    return value or None


def connection(settings: Any) -> dict[str, Any]:
    """Who files the issues: a connected account (with its login), a token, or nobody."""
    doc = _store(settings).get(TOKEN) or {}
    if not doc.get("value"):
        return {"kind": None, "login": None}
    return {"kind": doc.get("kind") or "token", "login": doc.get("login")}


def start_connect(settings: Any, *, post: Any = None) -> dict[str, Any]:
    """Start GitHub's device flow: a short code you enter on github.com/login/device."""
    cid = client_id(settings)
    if not cid:
        raise ValueError("No GitHub App is set up for this install ([bugs] github_client_id).")
    reply = (post or _post_form)(f"{WEB}/login/device/code", {"client_id": cid})
    if not reply.get("device_code"):
        raise RuntimeError(f"GitHub did not start the connection: {reply.get('error', '?')}")
    _store(settings).set(DEVICE, {"device_code": reply["device_code"]})
    return {
        "user_code": reply["user_code"],
        "verification_uri": reply.get("verification_uri") or f"{WEB}/login/device",
        "interval": int(reply.get("interval") or 5),
        "expires_in": int(reply.get("expires_in") or 900),
    }


def poll_connect(settings: Any, *, post: Any = None, get: Any = None) -> dict[str, Any]:
    """Ask GitHub once whether the code was entered. status: pending, slow_down, done,
    expired or denied. Done stores the account's token and its login."""
    cid, pending = client_id(settings), (_store(settings).get(DEVICE) or {}).get("device_code")
    if not cid or not pending:
        return {"status": "expired"}
    reply = (post or _post_form)(
        f"{WEB}/login/oauth/access_token",
        {"client_id": cid, "device_code": pending, "grant_type": DEVICE_GRANT},
    )
    error = reply.get("error")
    if error in ("authorization_pending", "slow_down"):
        return {"status": "pending" if error == "authorization_pending" else "slow_down"}
    _store(settings).set(DEVICE, {})
    if not reply.get("access_token"):
        return {"status": "denied" if error == "access_denied" else "expired"}
    token = reply["access_token"]
    login = (get or _get)(f"{API}/user", token).get("login")
    _store(settings).set(TOKEN, {"value": token, "kind": "account", "login": login})
    return {"status": "done", "login": login}


def disconnect(settings: Any) -> None:
    """Forget the account or token on this server (revoke the app on GitHub too, if you like)."""
    _store(settings).set(TOKEN, {})


def token_set(settings: Any) -> bool:
    return bool((SecretStore(settings.secrets_dir, SECRETS).get(TOKEN) or {}).get("value"))


def save_token(settings: Any, value: str) -> None:
    value = value.strip()
    if not value:
        raise ValueError("the token is empty")
    _store(settings).set(TOKEN, {"value": value, "kind": "token"})


def create(settings: Any, report: dict[str, Any], *, post: Any = None) -> dict[str, Any]:
    """Keep the report, then try to file it. Returns the stored report."""
    description = str(report.get("description") or "").strip()
    if not description:
        raise ValueError("describe what happened")
    rid = f"bug_{secrets.token_hex(6)}"
    stored: dict[str, Any] = {
        "id": rid,
        "created": utc_now().isoformat(),
        "description": description[:10_000],
        "page": str(report.get("page") or "")[:500],
        "errors": [str(e)[:1000] for e in (report.get("errors") or [])][:10],
        "failed_requests": [_request(r) for r in (report.get("failed_requests") or [])][:20],
        "browser": str(report.get("browser") or "")[:300],
        "version": __version__,
        "issue_url": None,
        "send_error": None,
        "screenshot": None,
    }
    shot = report.get("screenshot")
    if shot:
        stored["screenshot"] = _save_screenshot(settings, rid, str(shot))
    _write(settings, stored)
    return send(settings, rid, post=post)


def send(settings: Any, report_id: str, *, post: Any = None) -> dict[str, Any]:
    """File a kept report as an issue (again, after a failure). Needs the token."""
    report = load(settings, report_id)
    if report["issue_url"]:
        return report
    token = (SecretStore(settings.secrets_dir, SECRETS).get(TOKEN) or {}).get("value")
    if not token:
        report["send_error"] = "no GitHub account connected yet"
        _write(settings, report)
        return report
    title = report["description"].splitlines()[0][:80]
    try:
        reply = (post or _post)(
            f"{API}/repos/{repo(settings)}/issues",
            token,
            {"title": f"Bug: {title}", "body": issue_body(settings, report), "labels": ["bug"]},
        )
        report["issue_url"], report["send_error"] = reply.get("html_url"), None
    except RuntimeError as exc:
        report["send_error"] = str(exc)[:300]
        if " 401" in str(exc) and connection(settings)["kind"] == "account":
            report["send_error"] = (
                "GitHub no longer accepts the connection: connect your account again"
            )
    _write(settings, report)
    return report


def issue_body(settings: Any, report: dict[str, Any]) -> str:
    lines = [report["description"], "", "---", f"- Page: `{report['page'] or '-'}`"]
    lines += [f"- VibeSynergy {report['version']}", f"- Browser: {report['browser'] or '-'}"]
    lines.append(f"- Reported: {report['created']} (report `{report['id']}` on this server)")
    if report["errors"]:
        lines += ["", "**Shown on the page**", ""] + [f"> {e}" for e in report["errors"]]
    if report["failed_requests"]:
        lines += ["", "**Failed requests (latest first)**", ""]
        for r in report["failed_requests"]:
            lines.append(f"- `{r['method']} {r['url']}` → {r['status']}: {r['error']} ({r['at']})")
    if report["screenshot"]:
        lines += ["", f"Screenshot kept on this server: `{report['screenshot']}`"]
    return "\n".join(lines)


def listing(settings: Any) -> list[dict[str, Any]]:
    out = []
    for path in folder(settings).glob("bug_*.json"):
        r = json.loads(path.read_text())
        keys = ("id", "created", "description", "issue_url", "send_error", "page")
        out.append({k: r.get(k) for k in keys})
    return sorted(out, key=lambda r: r["created"], reverse=True)


def load(settings: Any, report_id: str) -> dict[str, Any]:
    if not _ID.fullmatch(report_id):
        raise ValueError("no such bug report")
    path = folder(settings) / f"{report_id}.json"
    if not path.exists():
        raise ValueError("no such bug report")
    return json.loads(path.read_text())


def _request(r: Any) -> dict[str, Any]:
    r = r if isinstance(r, dict) else {}
    return {
        "method": str(r.get("method") or "GET")[:10],
        "url": str(r.get("url") or "")[:300],
        "status": str(r.get("status") or "")[:10],
        "error": str(r.get("error") or "")[:300],
        "at": str(r.get("at") or "")[:40],
    }


def _save_screenshot(settings: Any, rid: str, data_url: str) -> str:
    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", data_url)
    if not match:
        raise ValueError("the screenshot must be a PNG, JPEG or WebP image")
    data = base64.b64decode(match.group(2))
    if len(data) > MAX_SCREENSHOT:
        raise ValueError("the screenshot is too large (8 MB at most)")
    path = folder(settings) / f"{rid}.{'jpg' if match.group(1) == 'jpeg' else match.group(1)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _write(settings: Any, report: dict[str, Any]) -> None:
    target = folder(settings) / f"{report['id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2))
    os.replace(tmp, target)


def _post(url: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "authorization": f"Bearer {token}",
            "accept": "application/vnd.github+json",
            "content-type": "application/json",
            "user-agent": f"vibesynergy/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as reply:
            return json.loads(reply.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = json.loads(exc.read() or b"{}").get("message", exc.reason)
        raise RuntimeError(f"GitHub said {exc.code}: {detail}") from exc
    except OSError as exc:
        raise RuntimeError(f"GitHub can't be reached: {exc}") from exc


def _get(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "authorization": f"Bearer {token}",
            "accept": "application/vnd.github+json",
            "user-agent": f"vibesynergy/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as reply:
            return json.loads(reply.read() or b"{}")
    except OSError as exc:
        raise RuntimeError(f"GitHub can't be reached: {exc}") from exc


def _post_form(url: str, body: dict[str, str]) -> dict[str, Any]:
    """The device-flow endpoints (no token yet); GitHub answers JSON when asked to."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": f"vibesynergy/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as reply:
            return json.loads(reply.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read() or b"{}") or {"error": f"http_{exc.code}"}
    except OSError as exc:
        raise RuntimeError(f"GitHub can't be reached: {exc}") from exc
