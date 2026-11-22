"""Portal host allow-list used for DNS-rebinding protection and OAuth return URLs."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from vibesynergy.core.config import DEFAULT_PORTAL_PORT

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _server(settings) -> dict:
    return getattr(settings, "server", None) or {}


def _host_of(url: str | None) -> str:
    return (urlsplit(url).hostname or "").lower() if url else ""


def _portal_hosts(settings) -> list[str]:
    """The names in ``[portal] hosts``, lowercased, without localhost and blanks."""
    names = [str(h).strip().lower() for h in (settings.portal.get("hosts", []) or [])]
    return [n for n in names if n and n not in LOCAL_HOSTS]


def _alias_names(settings) -> list[str]:
    """The names in ``[server] aliases``: an explicit list, never taken from a request."""
    names = [str(h).strip().lower().rstrip(".") for h in (_server(settings).get("aliases") or [])]
    return [n for n in names if n and n not in LOCAL_HOSTS]


def allowed_hosts(settings) -> set[str]:
    """Every host name the portal answers on: localhost, the canonical address, the tailnet
    name, ``[portal] hosts`` and ``[server] aliases``. All from the config file; a Host
    header outside this list is refused (421), so it never reaches link building."""
    hosts = set(LOCAL_HOSTS)
    hosts.add(_host_of(_server(settings).get("canonical_url")))
    hosts.add(_host_of(settings.public_url))
    hosts.update(_portal_hosts(settings))
    hosts.update(_alias_names(settings))
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


def canonical_url(settings) -> str | None:
    """The app's own address (#117): the base of every link VibeSynergy shows or sends to a
    person (mail, notices, push, login links, the Settings page, MCP ``portal_url``).

    ``[server] canonical_url`` when set; else the first domain in ``[portal] hosts`` (https,
    behind your reverse proxy); else the tailnet address (:func:`tailnet_address`). None when
    none is set. It never comes from the request, so a spoofed Host cannot change a link."""
    explicit = _server(settings).get("canonical_url")
    if explicit:
        return str(explicit).strip().rstrip("/")
    for name in _portal_hosts(settings):
        return f"https://{name}"
    return tailnet_address(settings)


# The older name: links to the portal and the canonical address are the same thing.
portal_address = canonical_url


def aliases(settings) -> list[str]:
    """The other addresses the portal is reached on, for "also reachable at" on Settings →
    System: the tailnet address, the other ``[portal] hosts`` and ``[server] aliases``, in
    that order, without the canonical one. They keep working; links never use them."""
    canonical = _host_of(canonical_url(settings))
    seen = {canonical, ""}
    found: list[str] = []
    tailnet = tailnet_address(settings)
    candidates: list[tuple[str, str]] = [(_host_of(tailnet), tailnet)] if tailnet else []
    candidates += [(n, f"https://{n}") for n in _portal_hosts(settings) + _alias_names(settings)]
    for host, url in candidates:
        if host in seen:
            continue
        seen.add(host)
        found.append(url)
    return found


def redirect_target(settings, host: str) -> str | None:
    """With ``[server] redirect_aliases = true``: the canonical origin to send a portal page
    request on an alias to; None to serve it where it is (the canonical host itself,
    localhost, or the setting off). ``host`` must already be an allowed name."""
    if _server(settings).get("redirect_aliases") is not True:
        return None
    base = canonical_url(settings)
    name = (host or "").lower()
    if not base or name in LOCAL_HOSTS or name == _host_of(base):
        return None
    return base


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
