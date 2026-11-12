"""Links to the portal (mail buttons, the Settings page) follow the addresses in use."""

from types import SimpleNamespace

from vibesynergy import alerts
from vibesynergy.portal.hosts import portal_address


def _settings(public_url=None, **portal):
    return SimpleNamespace(public_url=public_url, portal=portal)


def test_own_domain_wins_over_the_tailnet_address() -> None:
    settings = _settings(
        "https://box.example.ts.net", port=8467, hosts=["App.Example.org", "b.example.org"]
    )
    assert portal_address(settings) == "https://app.example.org"
    rt = SimpleNamespace(settings=settings)
    assert alerts.portal_url(rt, "#/coach/report/7") == "https://app.example.org/#/coach/report/7"


def test_without_a_domain_it_is_the_tailnet_name_on_the_portal_port() -> None:
    settings = _settings("https://box.example.ts.net", port=8467, hosts=[])
    assert portal_address(settings) == "https://box.example.ts.net:8467"
    assert (
        portal_address(_settings("https://box.example.ts.net")) == "https://box.example.ts.net:8447"
    )
    assert (
        portal_address(_settings("https://box.example.ts.net", hosts=["localhost"]))
        == "https://box.example.ts.net:8447"
    )


def test_no_address_at_all_gives_no_link() -> None:
    assert portal_address(_settings()) is None
    assert alerts.portal_url(SimpleNamespace(settings=_settings())) == ""


def test_google_redirect_uri_stays_on_the_tailnet_address_when_a_domain_is_added() -> None:
    """The URI is registered at Google; adding a domain must not move it."""
    from vibesynergy.portal.caldav_api import redirect_uri
    from vibesynergy.portal.hosts import tailnet_address

    plain = _settings("https://box.example.ts.net", port=8467)
    with_domain = _settings("https://box.example.ts.net", port=8467, hosts=["app.example.org"])
    expected = "https://box.example.ts.net:8467/api/caldav/google/callback"
    assert redirect_uri(plain) == redirect_uri(with_domain) == expected
    assert tailnet_address(with_domain) == "https://box.example.ts.net:8467"
    assert portal_address(with_domain) == "https://app.example.org"
    assert tailnet_address(_settings(hosts=["app.example.org"])) is None
    assert redirect_uri(_settings()) == "http://localhost:8447/api/caldav/google/callback"
