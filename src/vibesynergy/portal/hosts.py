"""Portal host allow-list used for DNS-rebinding protection and OAuth return URLs."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from vibesynergy.core.config import DEFAULT_PORTAL_PORT

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def allowed_hosts(settings) -> set[str]:
    hosts = set(LOCAL_HOSTS)
    if settings.public_url:
        hosts.add(urlsplit(settings.public_url).hostname or "")
    hosts.update(str(h).lower() for h in settings.portal.get("hosts", []))
    hosts.discard("")
    return hosts


def tailnet_address(settings) -> str | None:
    """The portal's tailnet address: the machine's name from ``[server] public_url`` on the
    portal port (tailscale serve publishes that port on the tailnet only). None without a
    public address. It does not change when a domain is added to ``[portal] hosts``, so
    anything registered elsewhere (an OAuth redirect URI) keeps working."""
    if not settings.public_url:
        return None
    host = urlsplit(settings.public_url).hostname
    return f"https://{host}:{settings.portal.get('port', DEFAULT_PORTAL_PORT)}" if host else None


def portal_address(settings) -> str | None:
    """Where the portal is reached, for links in mails and on the Settings page.

    With a domain of your own in ``[portal] hosts`` it is that domain (the first one), over
    https behind your reverse proxy. Without one it is the tailnet address
    (:func:`tailnet_address`). None when neither is set."""
    for host in settings.portal.get("hosts", []) or []:
        name = str(host).strip().lower()
        if name and name not in LOCAL_HOSTS:
            return f"https://{name}"
    return tailnet_address(settings)


def validate_portal_address(settings, portal: str) -> str:
    """Portal origin + path for OAuth return redirects (no credentials, query or fragment)."""
    if not portal.startswith(("https://", "http://")):
        raise ValueError("portal address must be http(s)")
    parsed = urlsplit(portal.split("#")[0].split("?")[0])
    if parsed.username or parsed.password:
        raise ValueError("portal address must not include credentials")
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts(settings):
        raise ValueError("portal address is not allowed")
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/") or "/", "", ""))


_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")


def normalize_hostname(value: str) -> str:
    """A portal (sub)domain as typed, checked and lowercased: ``vibesynergy.example.org``.
    No scheme, port, path or IP address; Unicode names become their xn-- form."""
    name = str(value or "").strip().lower().rstrip(".")
    if not name:
        raise ValueError("Enter a domain name, for example vibesynergy.example.org.")
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("That is not a valid domain name.") from exc
    try:
        ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        pass
    else:
        raise ValueError("Enter a domain name, not an IP address.")
    labels = name.split(".")
    if len(name) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(x) for x in labels):
        raise ValueError("That is not a valid domain name.")
    if labels[-1].isdigit():
        raise ValueError("That is not a valid domain name.")
    return name
