"""Videos of a workout (#114): reading them with ffmpeg and ffprobe.

A video is kept as it came (the original, in the blob store). What the portal and share links
play is a copy without the file's metadata (``-map_metadata -1``, so the place it was filmed
never reaches a share link), with the first video and audio track only (a phone's extra
tracks can carry the location too), remuxed into MP4 with ``faststart`` so it starts playing
before it is fully loaded. Nothing is re-encoded: the copy is quick and loses nothing. The
poster is one frame, as JPEG.

ffmpeg is a system program. Without it a video can't be taken in (``NoVideoTool``); photos are
not affected.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROBE_TIMEOUT_S = 60
COPY_TIMEOUT_S = 600
# A container ffprobe reads as one of these is a video VibeSynergy keeps.
_CONTAINERS = {"mov,mp4,m4a,3gp,3g2,mj2": "video/mp4"}
# Brands of the ISO file format that are pictures (HEIC, AVIF), not videos.
_IMAGE_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"mif1", b"msf1", b"avif", b"avis"}
# Atoms a QuickTime file without ``ftyp`` starts with.
_MOV_ATOMS = {b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"}


class NoVideoTool(RuntimeError):
    """ffmpeg or ffprobe is not installed: videos can't be taken in."""


class NotAVideo(ValueError):
    """ffprobe finds no video VibeSynergy can play in the file."""


@dataclass(frozen=True)
class Probe:
    content_type: str
    width: int
    height: int
    duration_s: float | None


def looks_like_video(head: bytes) -> bool:
    """Whether a file's first bytes are an MP4 or QuickTime video (not a HEIC picture)."""
    if len(head) < 12:
        return False
    box = head[4:8]
    if box == b"ftyp":
        return head[8:12] not in _IMAGE_BRANDS
    return box in _MOV_ATOMS


# Every file ffmpeg reads here is a local MP4/MOV: no other demuxer, no other protocol.
_INPUT = ("-protocol_whitelist", "file", "-f", "mov")


def _local(path: Path) -> str:
    return f"file:{path.resolve()}"


def _tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise NoVideoTool(f"{name} is not installed, so videos can't be taken in")
    return found


def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _run(args: list[str], timeout: float) -> bytes:
    try:
        done = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise NotAVideo("reading the video took too long") from exc
    if done.returncode != 0:
        raise NotAVideo("not a video VibeSynergy can read")
    return done.stdout


def probe(path: Path) -> Probe:
    """The video's size and length; ``NotAVideo`` when it has no video track."""
    out = _run(
        [
            _tool("ffprobe"),
            "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            *_INPUT, _local(path),
        ],
        PROBE_TIMEOUT_S,
    )  # fmt: skip
    try:
        info = json.loads(out or b"{}")
    except ValueError as exc:
        raise NotAVideo("not a video VibeSynergy can read") from exc
    container = str((info.get("format") or {}).get("format_name") or "")
    content_type = _CONTAINERS.get(container)
    streams = [
        s
        for s in info.get("streams") or []
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    if content_type is None or not streams:
        raise NotAVideo("choose an MP4 or MOV video")
    first = streams[0]
    width, height = int(first.get("width") or 0), int(first.get("height") or 0)
    rotation = _rotation(first)
    if rotation in (90, 270):
        width, height = height, width
    try:
        duration = float((info.get("format") or {}).get("duration") or first.get("duration"))
    except (TypeError, ValueError):
        duration = None
    if not width or not height:
        raise NotAVideo("the video has no picture")
    return Probe(content_type, width, height, duration)


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return abs(int(float(side["rotation"]))) % 360
            except (TypeError, ValueError):
                return 0
    try:
        return abs(int((stream.get("tags") or {}).get("rotate") or 0)) % 360
    except ValueError:
        return 0


def strip(src: Path, dst: Path) -> None:
    """The playable copy: first video and audio track, no metadata, MP4 with faststart."""
    _run(
        [
            _tool("ffmpeg"),
            "-nostdin", "-v", "error", "-y",
            *_INPUT, "-i", _local(src),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-c", "copy",
            # bitexact: no encoder tag, and the same copy every time it is made
            "-fflags", "+bitexact",
            "-movflags", "+faststart",
            "-f", "mp4",
            str(dst),
        ],
        COPY_TIMEOUT_S,
    )  # fmt: skip


def poster(src: Path, duration_s: float | None) -> bytes:
    """One frame as JPEG (turned upright, as players show it): a second in, or the middle of
    a shorter video."""
    at = min(1.0, (duration_s or 0) / 2)
    return _run(
        [
            _tool("ffmpeg"),
            "-nostdin", "-v", "error",
            "-ss", f"{at:.2f}",
            *_INPUT, "-i", _local(src),
            "-frames:v", "1",
            "-f", "image2", "-c:v", "mjpeg", "-q:v", "2",
            "pipe:1",
        ],
        PROBE_TIMEOUT_S,
    )  # fmt: skip


def remux_hls(playlist: Path, dst: Path, timeout: float = COPY_TIMEOUT_S) -> None:
    """A streamed video (HLS) whose playlist and parts were already downloaded next to
    ``playlist`` (by ``media_pull``, with verified TLS) put together into one MP4. ffmpeg
    reads local files only."""
    _run(
        [
            _tool("ffmpeg"),
            "-nostdin", "-v", "error", "-y",
            "-protocol_whitelist", "file,crypto",
            "-f", "hls", "-i", _local(playlist),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c", "copy",
            "-movflags", "+faststart",
            "-f", "mp4",
            str(dst),
        ],
        timeout,
    )  # fmt: skip
