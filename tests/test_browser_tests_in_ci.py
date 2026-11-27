"""Every browser test file runs in CI (#209): four once ran nowhere and went stale."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_browser_test_file_is_in_a_ci_step() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    browser = sorted(
        path.name
        for path in (ROOT / "tests").glob("test_*.py")
        if "sync_playwright" in path.read_text()
    )
    assert browser, "no browser tests found"
    missing = [name for name in browser if f"tests/{name}" not in workflow]
    assert not missing, f"browser test files in no CI step: {missing}"
