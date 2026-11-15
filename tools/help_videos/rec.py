# ruff: noqa: E501  (captions and injected scripts read best on one line)
"""Record short how-to videos of the portal, one per use case.

Each scenario drives a real browser against the throwaway demo portal, shows a visible
cursor, and logs a caption (nl + en) with its time. Output per scenario:
  out/<id>.webm (raw), out/<id>.nl.vtt, out/<id>.en.vtt, out/<id>.json (findings).
Usage: python run.py [scenario ...]   (all when none given)
"""

from __future__ import annotations

import json
import os
import shutil
import time
import traceback
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

# A throwaway demo portal (see README.md), never your own data.
H = Path(os.environ.get("VS_HELP_HOME", "~/vs-help-home")).expanduser()
OUT = H / "tour/out"
B = os.environ.get("VS_HELP_URL", "http://127.0.0.1:8591/")
W, HGT = 1280, 720

CURSOR_JS = """
(() => {
  if (window.__cur) return;
  const add = () => {
    const c = document.createElement('div');
    c.id = '__cur';
    c.style.cssText = 'position:fixed;left:-40px;top:-40px;width:22px;height:22px;border-radius:50%;'
      + 'background:rgba(255,190,0,.55);border:2px solid #b07800;z-index:2147483647;pointer-events:none;'
      + 'transition:left .45s ease, top .45s ease, transform .15s;transform:translate(-50%,-50%)';
    document.documentElement.appendChild(c);
    window.__cur = c;
  };
  if (document.documentElement) add(); else document.addEventListener('DOMContentLoaded', add);
})();
"""


class Rec:
    def __init__(self, page: Page, sid: str):
        self.pg, self.sid = page, sid
        self.t0 = time.monotonic()
        self.cues: list[tuple[float, str, str]] = []
        self.errors: list[str] = []
        self.notes: list[str] = []
        self.x, self.y = W / 2, HGT / 2
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {str(e)[:200]}"))
        page.on(
            "response",
            lambda r: (
                r.status >= 400
                and self.errors.append(
                    f"http {r.status} {r.request.method} {r.url[len(B) :][:100]}"
                )
            ),
        )
        page.on(
            "console",
            lambda m: m.type == "error" and self.errors.append(f"console: {m.text[:200]}"),
        )

    def now(self) -> float:
        return time.monotonic() - self.t0

    def say(self, nl: str, en: str, hold: float = 2.2) -> None:
        self.cues.append((self.now(), nl, en))
        self.pg.wait_for_timeout(int(hold * 1000))

    def wait(self, s: float = 0.8) -> None:
        self.pg.wait_for_timeout(int(s * 1000))

    def go(self, route: str) -> None:
        self.pg.goto(B + "#/" + route)
        self.pg.wait_for_load_state("networkidle")
        self.pg.evaluate(CURSOR_JS)
        self.wait(1.0)

    def _ensure_cursor(self) -> None:
        if not self.pg.evaluate("!!document.getElementById('__cur')"):
            self.pg.evaluate(CURSOR_JS)

    def point(self, loc, pause: float = 0.6):
        loc = loc.first if hasattr(loc, "first") else loc
        loc.scroll_into_view_if_needed()
        self.wait(0.3)
        box = loc.bounding_box()
        if box:
            self._ensure_cursor()
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            self.pg.evaluate(
                f"(() => {{const c=document.getElementById('__cur'); if(c){{c.style.left='{x}px';c.style.top='{y}px';}}}})()"
            )
            self.wait(pause)
        return loc

    def click(self, loc, pause: float = 0.6) -> None:
        loc = self.point(loc, pause)
        self.pg.evaluate(
            "(() => {const c=document.getElementById('__cur'); if(c){c.style.transform='translate(-50%,-50%) scale(.7)'; setTimeout(()=>c.style.transform='translate(-50%,-50%)',180)}})()"
        )
        loc.click()
        self.wait(0.9)

    def type(self, loc, text: str) -> None:
        loc = self.point(loc)
        loc.click()
        loc.press_sequentially(text, delay=45)
        self.wait(0.5)

    def select(self, loc, value: str) -> None:
        loc = self.point(loc)
        loc.select_option(label=value)
        self.wait(1.0)

    def scroll(self, dy: int, steps: int = 4) -> None:
        for _ in range(steps):
            self.pg.mouse.wheel(0, dy / steps)
            self.wait(0.25)
        self.wait(0.6)

    def check(self, cond: bool, msg: str) -> None:
        if not cond:
            self.notes.append("CHECK FAILED: " + msg)

    def text(self, sel: str = "main") -> str:
        return self.pg.locator(sel).first.inner_text()


def vtt(cues, total: float, lang: int) -> str:
    def ts(s: float) -> str:
        s = max(s, 0)
        return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"

    lines = ["WEBVTT", ""]
    for i, (t, *txt) in enumerate(cues):
        end = cues[i + 1][0] if i + 1 < len(cues) else total
        lines += [f"{ts(t)} --> {ts(max(end - 0.05, t + 0.5))}", txt[lang], ""]
    return "\n".join(lines)


SCENARIOS: dict[str, tuple[str, str, object]] = {}


def scenario(sid: str, nl: str, en: str, logged_in: bool = True):
    def deco(fn):
        SCENARIOS[sid] = (nl, en, fn, logged_in)
        return fn

    return deco


def run(ids: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/usr/bin/google-chrome")
        for sid in ids or list(SCENARIOS):
            nl, en, fn, logged_in = SCENARIOS[sid]
            vdir = OUT / f"raw-{sid}"
            shutil.rmtree(vdir, ignore_errors=True)
            ctx = browser.new_context(
                storage_state=str(H / "tour/state.json") if logged_in else None,
                viewport={"width": W, "height": HGT},
                record_video_dir=str(vdir),
                record_video_size={"width": W, "height": HGT},
                locale="nl-NL",
                timezone_id="Europe/Amsterdam",
            )
            ctx.add_init_script(CURSOR_JS)
            page = ctx.new_page()
            r = Rec(page, sid)
            ok = True
            try:
                fn(r)
                r.wait(1.5)
            except Exception as exc:  # keep going; the failure is a finding
                ok = False
                r.notes.append(
                    "SCENARIO FAILED: "
                    + "".join(traceback.format_exception_only(exc)).strip()[:400]
                )
                page.screenshot(path=str(OUT / f"{sid}-fail.png"))
            total = r.now()
            page.screenshot(path=str(OUT / f"{sid}-end.png"))
            video = page.video.path()
            ctx.close()
            shutil.move(video, OUT / f"{sid}.webm")
            shutil.rmtree(vdir, ignore_errors=True)
            (OUT / f"{sid}.nl.vtt").write_text(vtt(r.cues, total, 0))
            (OUT / f"{sid}.en.vtt").write_text(vtt(r.cues, total, 1))
            meta = {
                "id": sid,
                "nl": nl,
                "en": en,
                "ok": ok,
                "seconds": round(total, 1),
                "errors": r.errors,
                "notes": r.notes,
            }
            (OUT / f"{sid}.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
            print(
                f"{sid}: {'ok' if ok else 'FAILED'} {total:.0f}s errors={len(r.errors)} notes={r.notes} {r.errors[:4]}"
            )
        browser.close()
