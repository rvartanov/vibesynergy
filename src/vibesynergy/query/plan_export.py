"""The plan as files to keep offline: Excel and PDF (in place of the Google Sheet).

Both come from ``plan_model.export_rows`` (week notes and sessions with their step text) and
are built with the standard library: an .xlsx is a zip of a few XML parts, and the PDF uses
the built-in Courier font (every character equally wide, so lines wrap exactly). Nothing is
written anywhere; the portal hands the bytes to the browser.
"""

from __future__ import annotations

import io
import textwrap
import zipfile
from datetime import date
from typing import Any
from xml.sax.saxutils import escape

from sqlalchemy.engine import Engine

HEADERS = {
    "nl": ["Datum", "Week", "Fase", "Soort week", "Sport", "Naam", "Workout"],
    "en": ["Date", "Week", "Phase", "Week kind", "Sport", "Name", "Workout"],
}
COLUMNS = ["date", "week", "phase", "week_kind", "sport", "name", "workout"]
WIDTHS = [11, 6, 10, 10, 14, 34, 90]  # Excel column widths (characters)


def rows(engine: Engine) -> list[dict[str, Any]]:
    from vibesynergy.query import plan_model

    return plan_model.export_rows(engine)


# Excel --------------------------------------------------------------------------------------


def _cell(ref: str, value: Any, style: int = 0) -> str:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    text = escape(str(value or ""))
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _col(i: int) -> str:
    return chr(ord("A") + i)


def _pace(seconds: Any, per: str) -> str:
    if not isinstance(seconds, int | float):
        return ""
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}{per}"


SECTION_TITLES = {
    "races": {"nl": "Wedstrijden", "en": "Races"},
    "thresholds": {"nl": "Drempels en zones", "en": "Thresholds and zones"},
    "rules": {"nl": "Zonder coach", "en": "Without a coach"},
}


def extras(engine: Engine, today: date, lang: str = "nl") -> list[dict[str, Any]]:
    """What else the plan needs to train without VibeSynergy: the races, the thresholds with
    their zones, and the plan's own rules (the knowledge-base articles whose slug ends in
    ``-plan``)."""
    from vibesynergy.query import knowledge, plan, thresholds

    nl = lang == "nl"
    races = [
        [r["date"], r.get("priority") or "", r.get("title") or "", r.get("location") or ""]
        for r in plan.calendar_races(engine, today)
    ]
    values = {
        sport: {metric: row["value"] for metric, row in metrics.items()}
        for sport, metrics in thresholds.current_values(engine).items()
    }
    rows: list[list[Any]] = []
    for sport, metrics in values.items():
        for metric, value in metrics.items():
            shown = {
                "threshold_pace": _pace(value, "/km"),
                "css": _pace(value, "/100m"),
            }.get(metric, f"{value:g}")
            rows.append([sport, metric, shown, ""])
        for kind, found in thresholds.derived_zones(sport, metrics).items():
            for z in found.get("zones") or []:
                if kind == "pace":
                    per = "/100m" if sport == "swim" else "/km"
                    span = f"{_pace(z.get('slow'), per) or '...'} - {_pace(z.get('fast'), per)}"
                else:
                    span = f"{z.get('low') or '...'} - {z.get('high') or '...'}"
                rows.append([sport, f"{kind} zone {z['label']}", span, ""])
    rules = []
    for article in knowledge.list_articles(engine):
        if article["status"] != "active" or not article["slug"].endswith("-plan"):
            continue
        for rule in knowledge.get_article(engine, article["slug"]).get("rules") or []:
            if rule.get("active", True):
                rules.append([rule.get("id") or "", rule.get("text") or "", "", ""])
    head = {
        "races": ["Datum", "Prioriteit", "Wedstrijd", "Plaats"]
        if nl
        else ["Date", "Priority", "Race", "Place"],
        "thresholds": [
            "Sport",
            "Drempel of zone" if nl else "Threshold or zone",
            "Waarde" if nl else "Value",
            "",
        ],
        "rules": ["Regel" if nl else "Rule", "Tekst" if nl else "Text", "", ""],
    }
    return [
        {"key": key, "title": SECTION_TITLES[key][lang], "header": head[key], "rows": body}
        for key, body in (("races", races), ("thresholds", rows), ("rules", rules))
        if body
    ]


def _sheet(header: list[str], body: list[list[Any]], widths: list[int], *, notes: bool) -> str:
    head = "".join(_cell(f"{_col(i)}1", h, 1) for i, h in enumerate(header))
    lines = [f'<row r="1">{head}</row>']
    for n, row in enumerate(body, start=2):
        style = 3 if notes and not row[4] else 2  # a week note's row stands out
        cells = "".join(_cell(f"{_col(i)}{n}", v, style) for i, v in enumerate(row))
        lines.append(f'<row r="{n}">{cells}</row>')
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
        for i, w in enumerate(widths)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f"<cols>{cols}</cols><sheetData>{''.join(lines)}</sheetData></worksheet>"
    )


def xlsx(
    items: list[dict[str, Any]], lang: str = "nl", sections: list[dict[str, Any]] | None = None
) -> bytes:
    """The plan sheet (header frozen, week notes bold, text wrapped) and one sheet per extra
    section: races, thresholds and zones, the rules to train without a coach."""
    sheets = [
        (
            "Schema" if lang == "nl" else "Plan",
            _sheet(
                HEADERS[lang],
                [[item.get(key, "") for key in COLUMNS] for item in items],
                WIDTHS,
                notes=True,
            ),
        )
    ]
    for section in sections or []:
        widths = [12, 30, 60, 40] if section["key"] != "rules" else [10, 120, 4, 4]
        sheets.append(
            (section["title"], _sheet(section["header"], section["rows"], widths, notes=False))
        )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
        '<cellXfs count="4"><xf/>'
        '<xf fontId="1" applyFont="1"/>'
        '<xf applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
        '<xf fontId="1" applyFont="1" applyAlignment="1"><alignment vertical="top" '
        'wrapText="1"/></xf></cellXfs></styleSheet>'
    )
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{n}.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for n in range(1, len(sheets) + 1)
    )
    names = "".join(
        f'<sheet name="{escape(name[:31])}" sheetId="{n}" r:id="rId{n}"/>'
        for n, (name, _) in enumerate(sheets, start=1)
    )
    rels = "".join(
        f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/'
        f'2006/relationships/worksheet" Target="worksheets/sheet{n}.xml"/>'
        for n in range(1, len(sheets) + 1)
    )
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            f"{overrides}"
            '<Override PartName="/xl/styles.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{names}</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{rels}"
            f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'
        ),
        "xl/styles.xml": styles,
    }
    for n, (_, body) in enumerate(sheets, start=1):
        parts[f"xl/worksheets/sheet{n}.xml"] = body
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in parts.items():
            z.writestr(name, body)
    return out.getvalue()


# PDF ----------------------------------------------------------------------------------------

PAGE_W, PAGE_H = 842, 595  # A4 landscape, points
MARGIN = 36
SIZE = 8.5
LEAD = 10.5
CHARS = int((PAGE_W - 2 * MARGIN) / (SIZE * 0.6))  # Courier: 0.6 em per character
# Characters outside the PDF core fonts' WinAnsi set, written the way a printer can.
_PLAIN = {
    "\u2192": "->",  # right arrow
    "\u2190": "<-",
    "\u2264": "<=",
    "\u2265": ">=",
    "\u2248": "~",
    "\u2212": "-",  # minus sign
    "\u2011": "-",  # non-breaking hyphen
}


def _ansi(text: str) -> bytes:
    for a, b in _PLAIN.items():
        text = text.replace(a, b)
    data = text.encode("cp1252", "replace")
    return data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _lines(items: list[dict[str, Any]], lang: str, title: str) -> list[tuple[str, str]]:
    """(font, text) per printed line: F2 is bold for week headers and session names."""
    out: list[tuple[str, str]] = [("F2", title), ("F1", "")]
    for item in items:
        if not item.get("sport"):
            out.append(("F1", ""))
            week = f"{item['date']}  {item.get('name', '')}  {item.get('phase', '')}"
            out.append(("F2", week.strip()))
            for para in str(item.get("workout") or "").splitlines():
                for line in textwrap.wrap(para, CHARS - 2) or [""]:
                    out.append(("F1", "  " + line))
            continue
        day = date.fromisoformat(item["date"])
        names = ["ma", "di", "wo", "do", "vr", "za", "zo"] if lang == "nl" else [
            "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
        ]  # fmt: skip
        out.append(("F2", f"{names[day.weekday()]} {day:%d-%m}  {item['sport']}  {item['name']}"))
        for para in str(item.get("workout") or "").splitlines():
            if para.strip().startswith("[garmin]"):
                break  # the Garmin block is for the watch, not the page
            for line in textwrap.wrap(para, CHARS - 6) or [""]:
                out.append(("F1", "      " + line))
    return out


def _section_lines(section: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [("F1", ""), ("F2", section["title"])]
    for row in section["rows"]:
        cells = [str(v) for v in row if str(v)]
        if section["key"] == "rules":
            first, rest = cells[0], " ".join(cells[1:])
            wrapped = textwrap.wrap(rest, CHARS - 10) or [""]
            out.append(("F1", f"  {first:<7} {wrapped[0]}"))
            out.extend(("F1", " " * 10 + line) for line in wrapped[1:])
        else:
            out.append(("F1", "  " + "  ".join(cells)))
    return out


def pdf(
    items: list[dict[str, Any]],
    lang: str = "nl",
    title: str = "",
    sections: list[dict[str, Any]] | None = None,
) -> bytes:
    """A4 landscape pages of plain text: a bold line per week and per session, then the
    races, thresholds and zones, and the rules to train without a coach."""
    per_page = int((PAGE_H - 2 * MARGIN) / LEAD)
    lines = _lines(items, lang, title or ("Trainingsschema" if lang == "nl" else "Training plan"))
    for section in sections or []:
        lines.extend(_section_lines(section))
    pages = [lines[i : i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
    font2 = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold /Encoding /WinAnsiEncoding >>"
    )
    pages_id = len(objects) + 1 + 2 * len(pages)  # after every page and its content
    kids = []
    for number, page in enumerate(pages, start=1):
        stream = io.BytesIO()
        y = PAGE_H - MARGIN
        for font, text in page:
            stream.write(b"BT /%s %.1f Tf %d %.1f Td (" % (font.encode(), SIZE, MARGIN, y))
            stream.write(_ansi(text))
            stream.write(b") Tj ET\n")
            y -= LEAD
        foot = f"{number}/{len(pages)}"
        stream.write(
            b"BT /F1 7 Tf %d %d Td (%s) Tj ET\n"
            % (PAGE_W - MARGIN - 30, MARGIN // 2, foot.encode())
        )
        body = stream.getvalue()
        content = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(body), body))
        kids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
                b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>"
                % (pages_id, PAGE_W, PAGE_H, content, font1, font2)
            )
        )
    refs = b" ".join(b"%d 0 R" % k for k in kids)
    pid = add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (refs, len(kids)))
    if pid != pages_id:
        raise RuntimeError("PDF page tree out of order")
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (i, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, catalog, xref)
    )
    return out.getvalue()
