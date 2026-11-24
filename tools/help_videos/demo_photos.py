"""Photos for the "workout-photos" help video, and taking them out again (#114).

The demo has no Strava or Garmin login, so nothing comes in by itself. ``seed`` puts one
synthetic photo "from Garmin" on the newest demo workout (as the photo pull would take it
in) and writes the synthetic photo the video adds to ``~/vs-help-home/tour/photo.jpg``.
``clear`` takes out every photo and its history, so other videos and the next recording
start from the same demo.

Only for the help-video demo home (``~/vs-help-home``), never for real data. Run with the
demo's XDG variables::

    XDG_CONFIG_HOME=~/vs-help-home/config XDG_DATA_HOME=~/vs-help-home/data \\
        uv run python tools/help_videos/demo_photos.py seed
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw
from sqlalchemy import delete, select

from vibesynergy.core.config import load_settings
from vibesynergy.query import workout_media
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import (
    activity_unified,
    media_change,
    media_check,
    media_item,
    media_origin,
)

DEMO_HOME = Path.home() / "vs-help-home"
UPLOAD = DEMO_HOME / "tour" / "photo.jpg"


def _guard() -> None:
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).expanduser().resolve().is_relative_to(DEMO_HOME):
        sys.exit("refused: set XDG_CONFIG_HOME/XDG_DATA_HOME to the demo home (~/vs-help-home)")


def landscape(
    sky: tuple[int, int, int], hill: tuple[int, int, int], sun_x: int, mirror: bool = False
) -> bytes:
    """A drawn landscape (sky, sun, hills and a path): synthetic, nobody's photo."""
    w, h = 1200, 900
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        k = y / h
        draw.line([(0, y), (w, y)], fill=tuple(int(c + (255 - c) * k * 0.6) for c in sky))
    draw.ellipse([sun_x - 70, 140, sun_x + 70, 280], fill=(255, 214, 120))
    draw.polygon(
        [(0, 620), (300, 470), (620, 600), (900, 430), (1200, 560), (1200, 900), (0, 900)],
        fill=hill,
    )
    darker = tuple(max(0, c - 40) for c in hill)
    draw.polygon(
        [(0, 760), (400, 640), (800, 720), (1200, 650), (1200, 900), (0, 900)], fill=darker
    )
    draw.polygon([(560, 900), (640, 900), (610, 650), (596, 650)], fill=(214, 196, 160))
    if mirror:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()


def seed(rt: Runtime) -> None:
    with rt.engine.connect() as conn:
        newest = conn.scalar(
            select(activity_unified.c.group_id).order_by(activity_unified.c.start_utc.desc())
        )
    if newest is None:
        sys.exit("the demo has no workouts: run vibesynergy sync first")
    workout_media.add(
        rt.engine,
        rt.raw.blobs,
        newest,
        landscape((40, 90, 170), (60, 120, 70), 900),
        source="garmin",
        origin=("demo-1", newest.split(":", 1)[1]),
    )
    UPLOAD.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD.write_bytes(landscape((220, 120, 80), (90, 110, 60), 300, mirror=True))


def clear(rt: Runtime) -> None:
    with rt.engine.begin() as conn:
        for table in (media_item, media_origin, media_change, media_check):
            conn.execute(delete(table))
    UPLOAD.unlink(missing_ok=True)


def main() -> None:
    _guard()
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in ("seed", "clear"):
        sys.exit("usage: demo_photos.py seed|clear")
    rt = Runtime.open(load_settings(None))
    try:
        clear(rt)
        if action == "seed":
            seed(rt)
    finally:
        rt.close()


if __name__ == "__main__":
    main()
