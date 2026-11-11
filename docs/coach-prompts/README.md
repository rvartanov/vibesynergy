# Coach routine prompts: seed only

The live prompts are in the VibeSynergy knowledge base, one article per routine under
topic `coach-prompts`: `coach-prompt-morning`, `coach-prompt-evening`,
`coach-prompt-weekly` and `coach-prompt-insight`. The athlete edits them in the portal under
Coach → Methods & rules; every edit is a version in Coach → Change log, with a diff and a
revert. Routines and the API coach runner read them with the MCP tool
`vibesynergy_coach_prompt(job)` (or `vibesynergy_kb_get(slug=…)`), or in code with
`vibesynergy.query.coach_prompts.prompt(engine, job)`.

The `*.md` files here are only the seed. Copy them into the knowledge base with
`uv run vibesynergy kb seed-prompts` or the button on the empty Methods & rules section. Seeding
is safe to repeat: it adds a missing prompt, updates one that still holds an earlier seed,
and leaves a prompt alone once it was edited in the portal or over MCP (the knowledge base
wins). The leading `<!-- … -->` copy note is not copied. While an article is missing or
archived, `vibesynergy_coach_prompt` falls back to the file here.

## Generic seeds and an install's own

The files here are **generic**: no person, races or plan rules. An install keeps its own
seeds in `<data_dir>/coach-prompts/<job>.md`; `kb seed-prompts`, the portal button and the
fallback while an article is missing read that folder first and only then this one. The
live prompts stay in the knowledge base either way.
