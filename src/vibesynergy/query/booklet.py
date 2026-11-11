"""The coach booklet ( part 4): the plan on paper, readable without VibeSynergy or a coach.

One printable HTML page in the athlete's language: the races, the phases, every week with
its focus and hours, the thresholds the workouts use, and the plan's decision rules (the
active rules of the plan article in the knowledge base). It is built from what VibeSynergy
already holds; nothing is written.
"""

from __future__ import annotations

import html
from datetime import date
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.i18n import translator
from vibesynergy.store.schema import kb_article, kb_rule

_UNITS = {"W": "W", "bpm": "bpm"}
_METRICS = {
    "ftp": "FTP",
    "lthr": "LTHR",
    "max_hr": "Max HR",
    "threshold_pace": "Threshold pace",
    "css": "CSS",
}
_SPORTS = {"bike": "Cycling", "run": "Running", "swim": "Swimming"}
INTRO = (
    "Made on {date} from your plan in VibeSynergy. Keep it with you: it works without VibeSynergy "
    "and without a coach."
)
VALUES_NOTE = "The workouts use these values; after a test they change."


def _value(value: float, unit: str | None) -> str:
    if unit == "s/km":
        return f"{int(value) // 60}:{int(value) % 60:02d} /km"
    if unit == "s/100m":
        return f"{int(value) // 60}:{int(value) % 60:02d} /100 m"
    return f"{value:.0f} {_UNITS.get(unit or '', unit or '')}".strip()


def _plan_rules(engine: Engine) -> list[dict[str, str]]:
    """Active rules of the plan article (slug ending in -plan)."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(kb_rule.c.id, kb_rule.c.text)
            .join(kb_article, kb_article.c.id == kb_rule.c.article_id)
            .where(
                kb_article.c.status == "active",
                kb_article.c.slug.like("%-plan"),
                kb_rule.c.active.is_(True),
            )
            .order_by(kb_rule.c.id)
        ).all()
    return [{"id": r.id, "text": r.text} for r in rows]


def data(engine: Engine, today: date) -> dict[str, Any]:
    from vibesynergy.query import plan_model, thresholds, training_plan

    overview = training_plan.overview(engine, today)
    model = plan_model.summary(engine) or {}
    return {
        "name": model.get("name") or "",
        "today": today.isoformat(),
        "races": overview["races"],
        "phases": overview["phases"],
        "weeks": overview["weeks"],
        "thresholds": thresholds.current_values(engine),
        "rules": _plan_rules(engine),
    }


def render(info: dict[str, Any], lang: str = "nl") -> str:
    """The booklet as a self-contained HTML page (prints on a few A4 pages)."""
    t = translator(lang)
    e = html.escape
    title = t("Coach booklet")
    if info["name"]:
        title = f"{title}: {info['name']}"
    out = [
        "<!doctype html>",
        f'<html lang="{e(lang)}"><head><meta charset="utf-8">',
        f"<title>{e(title)}</title>",
        "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:46rem;margin:2rem auto;"
        "padding:0 1rem;color:#111}h1{font-size:1.6rem}h2{margin-top:2rem;font-size:1.2rem;"
        "border-bottom:1px solid}h3{font-size:1rem;margin:1rem 0 .2rem}"
        "p,li{margin:.2rem 0}.sub{color:#444}table{border-collapse:collapse}"
        "td,th{padding:.2rem .6rem;text-align:left;border-bottom:1px solid #ddd}"
        "@media print{body{margin:0}h2{break-after:avoid}.week{break-inside:avoid}}</style>",
        "</head><body>",
        f"<h1>{e(title)}</h1>",
        f'<p class="sub">{e(t(INTRO, date=info["today"]))}</p>',
    ]
    if info["races"]:
        out.append(f"<h2>{e(t('Races'))}</h2><ul>")
        for race in info["races"]:
            prio = f" ({e(race['priority'])})" if race.get("priority") else ""
            out.append(f"<li>{e(str(race['date']))}: {e(race.get('title') or '')}{prio}</li>")
        out.append("</ul>")
    if info["phases"]:
        out.append(
            f"<h2>{e(t('Phases'))}</h2><table><tr><th>{e(t('Phase'))}</th>"
            f"<th>{e(t('From'))}</th><th>{e(t('To'))}</th></tr>"
        )
        for phase in info["phases"]:
            out.append(
                f"<tr><td>{e(phase.get('phase') or '')}</td><td>{e(str(phase['start']))}</td>"
                f"<td>{e(str(phase['end']))}</td></tr>"
            )
        out.append("</table>")
    values = info["thresholds"]
    if values:
        out.append(f"<h2>{e(t('Your thresholds'))}</h2>")
        out.append(f'<p class="sub">{e(t(VALUES_NOTE))}</p><ul>')
        for sport, metrics in values.items():
            parts = [
                f"{t(_METRICS.get(k) or k)} {_value(v['value'], v.get('unit'))}"
                for k, v in metrics.items()
                if v.get("value") is not None
            ]
            name = t(_SPORTS.get(sport) or sport)
            out.append(f"<li><b>{e(name)}</b>: {e(' · '.join(parts))}</li>")
        out.append("</ul>")
    if info["rules"]:
        out.append(f"<h2>{e(t('Rules of the plan'))}</h2><ul>")
        for rule in info["rules"]:
            out.append(f"<li><b>{e(rule['id'])}</b> {e(rule['text'])}</li>")
        out.append("</ul>")
    if info["weeks"]:
        out.append(f"<h2>{e(t('Weeks'))}</h2>")
        for week in info["weeks"]:
            out.append('<div class="week">')
            out.append(
                f'<h3>{e(week.get("title") or "")} <span class="sub">'
                f"{e(str(week['start']))} \u2013 {e(str(week['end']))}</span></h3>"
            )
            for para in (week.get("text") or "").split("\n"):
                if para.strip():
                    out.append(f"<p>{e(para.strip())}</p>")
            out.append("</div>")
    out.append("</body></html>")
    return "\n".join(out)
