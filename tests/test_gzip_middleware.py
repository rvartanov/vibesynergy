"""GZip on the portal and API apps (not MCP)."""

from fastapi.testclient import TestClient

from vibesynergy.api.app import create_app
from vibesynergy.portal.app import create_portal_app


def test_portal_json_is_gzipped(runtime):
    client = TestClient(create_portal_app(runtime), base_url="http://127.0.0.1:8447")
    reply = client.get("/static/js/app.js", headers={"Accept-Encoding": "gzip"})
    assert reply.status_code == 200
    assert reply.headers.get("content-encoding") == "gzip"
    assert len(reply.content) > 500


def test_api_mcp_path_is_not_gzipped(runtime):
    app = create_app(runtime, with_mcp=True)
    client = TestClient(app, base_url="http://127.0.0.1:8448")
    reply = client.get("/mcp", headers={"Accept-Encoding": "gzip"})
    assert reply.status_code in (404, 405, 406)
    assert reply.headers.get("content-encoding") != "gzip"
