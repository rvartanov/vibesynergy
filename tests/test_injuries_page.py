"""Health → Complaints & injuries."""

from __future__ import annotations

from fastapi.testclient import TestClient

from vibesynergy.portal.app import create_portal_app


def test_injuries_api_lists_open_and_resolved_in_the_chosen_language(runtime) -> None:
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    headers = {"x-vibesynergy": "portal"}
    created = api.post(
        "/api/diary/injury",
        json={"body_part": "calf", "side": "right", "kind": "niggle", "pain": 5},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    data = api.get("/api/injuries", params={"lang": "nl"}).json()["data"]
    (item,) = data["injuries"]
    assert item["status"] != "resolved" and item["latest_pain"] == 5
    assert any("Pijn" in flag["message"] for flag in item["flags"])  # pain above 3, in Dutch
