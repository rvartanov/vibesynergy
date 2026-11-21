"""A readable line diff: the changed lines with a little context, long unchanged runs folded.

Used by Coach → Change log (one version against the one before) and by a knowledge-base
proposal (the proposed text against the current one, #137), so the athlete reads what
changes instead of the whole article.
"""

from __future__ import annotations

import difflib

CONTEXT_LINES = 2


def lines(before: str, after: str, *, context: int = CONTEXT_LINES) -> list[dict[str, str]]:
    """``[{"op": "same" | "del" | "add" | "gap", "text": ...}]``. A ``gap`` holds the number
    of unchanged lines folded away."""
    old, new = before.splitlines(), after.splitlines()
    out: list[dict[str, str]] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            same = old[i1:i2]
            if len(same) > 2 * context + 1:
                head = same[:context] if i1 > 0 else []
                tail = same[-context:] if i2 < len(old) else []
                out.extend({"op": "same", "text": line} for line in head)
                out.append({"op": "gap", "text": str(len(same) - len(head) - len(tail))})
                out.extend({"op": "same", "text": line} for line in tail)
            else:
                out.extend({"op": "same", "text": line} for line in same)
            continue
        out.extend({"op": "del", "text": line} for line in old[i1:i2])
        out.extend({"op": "add", "text": line} for line in new[j1:j2])
    return out
