"""Model-neutral text: no AI model or provider is named where the role is meant (#96).

The AI that coaches is the **AI coach** (Dutch: AI-coach) in every text a model or the
athlete reads; an **AI app** is the MCP client it may work in. Which model or provider does
the work is a setting (Coach → Settings), never part of a prompt, a rule or a tool
description, so another model works just as well. The glossary is in docs/AI_HARNESS.md.

``named_models`` is the one check behind the guard test (tests/test_model_neutral.py) and
the knowledge-base lint (``knowledge.lint``).
"""

from __future__ import annotations

import re

# Model families, providers and their products. Whole words, any case.
MODEL_NAMES = (
    "Claude",
    "Anthropic",
    "Opus",
    "Sonnet",
    "Haiku",
    "Fable",
    "GPT",
    "ChatGPT",
    "OpenAI",
    "Codex",
    "Gemini",
    "Bard",
    "Grok",
    "xAI",
    "Llama",
    "Ollama",
    "Mistral",
    "DeepSeek",
    "Qwen",
    "Copilot",
)
_NAMED = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(re.escape(n) for n in MODEL_NAMES) + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_CANONICAL = {name.casefold(): name for name in MODEL_NAMES}


def named_models(text: str | None) -> list[str]:
    """The model and provider names in a text, each once, in order of appearance.
    ``gpt-5`` and ``claude-coach-loop`` count; ``sonnetten`` does not."""
    found: list[str] = []
    for match in _NAMED.finditer(text or ""):
        name = _CANONICAL[match.group(1).casefold()]
        if name not in found:
            found.append(name)
    return found
