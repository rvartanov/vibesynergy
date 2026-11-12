"""MCP tools: knowledge (split from mcp/server.py)."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from vibesynergy.mcp.identity import current_author
from vibesynergy.mcp.server import (
    READ,
    WRITE,
    ToolKit,
)
from vibesynergy.query import (
    knowledge,
)


def register(kit: ToolKit) -> None:
    engine = kit.engine
    tool = kit.tool
    envelope = kit.envelope

    # Knowledge ---------------------------------------------------------------------------

    @tool(READ, "read")
    def vibesynergy_kb_search(query: str, topic: str | None = None) -> dict[str, Any]:
        """Search articles, rules and sources. ``topic`` keeps hits from that subject only."""
        return envelope({"hits": knowledge.search(engine, query, topic=topic)})

    @tool(READ, "read")
    def vibesynergy_kb_get(slug: str | None = None, rule_id: str | None = None) -> dict[str, Any]:
        """One article by slug, or one rule by id (for example HRV-2), with every older version."""
        try:
            if rule_id:
                return envelope(knowledge.get_rule(engine, rule_id))
            if slug:
                return envelope(knowledge.get_article(engine, slug))
        except LookupError as exc:
            raise ToolError(str(exc)) from exc
        raise ToolError("pass slug or rule_id")

    @tool(READ, "read")
    def vibesynergy_coach_prompt(
        job: Literal[
            "morning",
            "evening",
            "weekly",
            "monthly",
            "block",
            "race",
            "test",
            "thresholds",
            "kb",
            "injury",
            "checkin",
            "insight",
        ],
    ) -> dict[str, Any]:
        """The current instructions for one coach routine (morning, evening, weekly review or
        post-activity insight). The athlete edits them in the portal; read them at the start
        of every run and follow them. ``version`` changes with every edit."""
        from vibesynergy.query import coach_prompts

        try:
            return envelope(
                coach_prompts.prompt(engine, job, overlay=coach_prompts.overlay_dir(kit.settings))
            )
        except LookupError as exc:
            raise ToolError(str(exc)) from exc

    @tool(WRITE, "coach.write")
    def vibesynergy_kb_upsert(
        slug: str,
        title: str | None = None,
        body_md: str | None = None,
        topic: str | None = None,
        evidence_grade: str | None = None,
        status: str | None = None,
        rules: list[knowledge.KbRuleInput] | None = None,
        sources: list[knowledge.KbSourceInput] | None = None,
        revert_to: int | None = None,
    ) -> dict[str, Any]:
        """Write an article and, when sent, its rules and sources.

        Each change keeps the old version.

        Author comes from the connection. ``revert_to`` copies that article version forward.
        Omit rules or sources to leave them as they are.

        The coach's own instructions (topic ``coach-prompts``) are not written here: suggest
        the change with ``vibesynergy_kb_propose_change`` and the athlete approves it.
        """
        if knowledge.is_prompt_article(engine, slug, topic):
            # These articles are what the unattended coach is told to do. An app that may
            # write messages must not be able to rewrite them on its own.
            raise ToolError(
                "The coach's instructions change only with the athlete's approval. Suggest "
                "this with vibesynergy_kb_propose_change (kind 'article')."
            )
        try:
            saved = knowledge.upsert_article(
                engine,
                slug=slug,
                author=current_author(),
                title=title,
                body_md=body_md,
                topic=topic,
                evidence_grade=evidence_grade,
                status=status,
                rules=[item.model_dump() for item in rules] if rules is not None else None,
                sources=[item.model_dump() for item in sources] if sources is not None else None,
                revert_to=revert_to,
            )
        except LookupError as exc:
            raise ToolError(str(exc)) from exc
        return envelope(saved)

    @tool(WRITE, "plan.propose")
    def vibesynergy_kb_propose_change(
        kind: Literal["article", "rule", "source"],
        target: str,
        rationale: str,
        title: str | None = None,
        body_md: str | None = None,
        topic: str | None = None,
        evidence_grade: str | None = None,
        status: str | None = None,
        new_text: str | None = None,
        grade: str | None = None,
        active: bool | None = None,
        citation: str | None = None,
        doi: str | None = None,
        url: str | None = None,
        verified_at: str | None = None,
        article_slug: str | None = None,
    ) -> dict[str, Any]:
        """Suggest a change. The current text stays until the athlete approves it in the portal.

        For a rule, pass ``target`` as the rule id and the replacement in ``new_text``.
        """
        changes = {
            "title": title,
            "body_md": body_md,
            "topic": topic,
            "evidence_grade": evidence_grade,
            "status": status,
            "text": new_text,
            "grade": grade,
            "active": active,
            "citation": citation,
            "doi": doi,
            "url": url,
            "verified_at": verified_at,
            "article_slug": article_slug,
        }
        try:
            proposal = knowledge.propose_change(
                engine,
                kind=kind,
                target=target,
                rationale=rationale,
                author=current_author(),
                changes=changes,
            )
        except LookupError as exc:
            raise ToolError(str(exc)) from exc
        return envelope(proposal)
