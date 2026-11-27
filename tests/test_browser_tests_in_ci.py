"""Every browser test file runs in CI (#209): four once ran nowhere and went stale.

CONTRIBUTING.md lists the same commands, so it cannot fall behind CI either (#204).
"""

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
    contributing = (ROOT / "CONTRIBUTING.md").read_text()
    undocumented = [name for name in browser if f"tests/{name}" not in contributing]
    assert not undocumented, f"browser test files not in CONTRIBUTING.md: {undocumented}"
