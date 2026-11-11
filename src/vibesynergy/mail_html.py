"""The alert and coach mail as HTML: sections, colour codes and symbols.

The plain-text body stays as the first part of every mail; this is the alternative part a
mail app shows when it can. Colours and symbols are those of the portal's chips:
green keep/done, amber adjust, grey skip, red missed or stop, blue proposal or note. All
styles are inline (mail apps drop style sheets) and every colour has a symbol and a word,
so nothing depends on colour alone.
"""

from __future__ import annotations

import html
import re
from typing import Any

# decision -> (symbol, label, text colour, background)
DECISIONS = {
    "keep": ("✓", "Keep", "#1b6b33", "#e3f4e8"),
    "done": ("✓", "Done", "#1b6b33", "#e3f4e8"),
    "adjust": ("~", "Adjust", "#7a4d00", "#fff1d6"),
    "skip": ("\u2013", "Skip", "#4a4f57", "#eceef1"),
    "missed": ("!", "Missed", "#a3221b", "#fde6e4"),
    "proposal": ("?", "Proposal", "#1d4f91", "#e3edfb"),
}
# alert level -> (symbol, label, colour, background)
LEVELS = {
    "stop": ("!", "STOP", "#a3221b", "#fde6e4"),
    "warn": ("▲", "Attention", "#7a4d00", "#fff1d6"),
    "info": ("i", "Note", "#1d4f91", "#e3edfb"),
}
# "Label: text" lines in the coach's details become their own rows (Besluit, Onderbouwing,
# Morgen, Dag 9, Horizon ...); the label is kept as the coach wrote it.
_LABELLED = re.compile(r"^([A-ZÀ-Ý][^:\n]{1,40}?(?:\([^)]*\))?):\s+(.+)$")


def _chip(symbol: str, label: str, ink: str, wash: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:999px;'
        f'background:{wash};color:{ink};font-weight:600;font-size:13px;white-space:nowrap">'
        f"{html.escape(symbol)} {html.escape(label)}</span>"
    )


def _section(title: str, body: str) -> str:
    return (
        f'<h3 style="margin:20px 0 6px;font-size:15px;color:#111">{html.escape(title)}</h3>{body}'
    )


def _details(text: str, tr: Any) -> str:
    rows, loose = [], []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = _LABELLED.match(line)
        if match:
            rows.append(
                f'<tr><td style="padding:3px 12px 3px 0;vertical-align:top;color:#444;'
                f'white-space:nowrap">{html.escape(match.group(1))}</td>'
                f'<td style="padding:3px 0;color:#222">{html.escape(match.group(2))}</td></tr>'
            )
        else:
            loose.append(f'<p style="margin:4px 0;color:#222">{html.escape(line)}</p>')
    table = f'<table style="border-collapse:collapse;font-size:14px">{"".join(rows)}</table>'
    return _section(tr("Details"), (table if rows else "") + "".join(loose))


def _lead(text: str) -> str:
    """The report's text, line by line (8-11: the lines ran together in one paragraph). The
    first line is the advice, a little larger; "Herstel: …" lines get their heading in bold."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    out = [
        f'<p style="margin:0 0 10px;font-size:16px;line-height:1.5;color:#111">'
        f"{html.escape(lines[0])}</p>"
    ]
    for line in lines[1:]:
        match = re.match(r"^([^:]{2,24}):\s+(.+)$", line)
        if match:
            out.append(
                f'<p style="margin:4px 0;color:#222"><b>{html.escape(match.group(1))}:</b> '
                f"{html.escape(match.group(2))}</p>"
            )
        else:
            out.append(f'<p style="margin:4px 0;color:#222">{html.escape(line)}</p>')
    return "".join(out)


def _coach(record: dict[str, Any], tr: Any) -> str:
    rich = record.get("rich") or {}
    parts = [_lead(rich.get("text") or "")]
    sessions = rich.get("sessions") or []
    if sessions:
        rows = []
        for s in sessions:
            symbol, label, ink, wash = DECISIONS.get(
                s.get("decision") or "", ("•", s.get("decision") or "", "#4a4f57", "#eceef1")
            )
            note = s.get("note") or ""
            rows.append(
                f'<tr><td style="padding:6px 12px 6px 0;vertical-align:top">'
                f"{_chip(symbol, tr(label), ink, wash)}</td>"
                f'<td style="padding:6px 0;color:#111"><b>{html.escape(s.get("title") or "")}</b>'
                f"{f'<br><span style="color:#444">{html.escape(note)}</span>' if note else ''}"
                f"</td></tr>"
            )
        parts.append(
            _section(
                tr("Today"), f'<table style="border-collapse:collapse">{"".join(rows)}</table>'
            )
        )
    if rich.get("details"):
        parts.append(_details(rich["details"], tr))
    return "".join(parts)


def _alert(record: dict[str, Any], tr: Any) -> str:
    symbol, label, ink, wash = LEVELS.get(record.get("level") or "info", LEVELS["info"])
    detail = str(record.get("detail") or "").strip()
    return (
        f'<p style="margin:8px 0">{_chip(symbol, tr(label), ink, wash)} '
        f'<span style="color:#111">{html.escape(record.get("text") or "")}</span></p>'
        + (
            f'<p style="margin:2px 0 8px;color:#444;font-size:13px">{html.escape(detail)}</p>'
            if detail
            else ""
        )
    )


def render(records: list[dict[str, Any]], tr: Any, *, title: str, link: str | None) -> str:
    """One mail: each coach note in full, then the other alerts, then a link to the portal."""
    coach = [r for r in records if r.get("kind") == "daily_coach" and r.get("rich")]
    others = [r for r in records if r not in coach]
    body = []
    for record in coach:
        body.append(_coach(record, tr))
    if others:
        if coach:
            body.append(_section(tr("Also"), ""))
        seen: set[str] = set()
        for record in others:
            key = " ".join(str(record.get("text") or "").split()).casefold()
            if key in seen:
                continue
            seen.add(key)
            body.append(_alert(record, tr))
    button = (
        f'<p style="margin:24px 0 0"><a href="{html.escape(link)}" style="display:inline-block;'
        f"padding:8px 16px;border-radius:8px;background:#175bc4;color:#fff;text-decoration:none;"
        f'font-weight:600">{html.escape(tr("Open in VibeSynergy"))}</a></p>'
        if link
        else ""
    )
    return (
        '<!doctype html><html><body style="margin:0;background:#f4f5f7">'
        '<div style="max-width:600px;margin:0 auto;padding:20px;font:15px/1.5 '
        '-apple-system,Segoe UI,Roboto,sans-serif;background:#fff">'
        f'<h2 style="margin:0 0 12px;font-size:18px;color:#111">{html.escape(title)}</h2>'
        + "".join(body)
        + button
        + '<p style="margin:24px 0 0;color:#666;font-size:12px">VibeSynergy</p></div></body></html>'
    )
