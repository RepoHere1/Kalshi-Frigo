"""Cloddsbot skills: small operator rule sheets injected into the AI prompts.

The operator's "skills" concept, applied to the trading brain: short markdown
sheets under `prompts/skills/` appended to the job prompts (veto, sentinel).
They carry only rules the rest of the system already enforces or the operator
has explicitly ordered - never new trade logic - and each sheet is capped so
a prompt budget cannot silently balloon. `CLODDSBOT_SKILLS=0` turns every
injection into a no-op.

Loading is cached; a missing sheet degrades to an empty string so a deleted
file can never break a trading cycle.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

SKILLS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "skills"
MAX_SKILL_CHARS = 2000
HEADER = "\n\n=== OPERATOR SKILLS (authoritative trading rules) ===\n"

_cache: Dict[str, str] = {}


def skills_enabled() -> bool:
    """The kill switch: CLODDSBOT_SKILLS=0 disables every injection."""
    return os.environ.get("CLODDSBOT_SKILLS", "1").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


def load_skill(name: str) -> str:
    """One sheet's text, stripped and capped; '' when missing or unreadable."""
    if name in _cache:
        return _cache[name]
    try:
        text = (SKILLS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    text = text[:MAX_SKILL_CHARS]
    _cache[name] = text
    return text


def skills_block(*names: str) -> str:
    """The injectable block for a prompt. Empty when disabled or none exist.

    The block is clearly delimited so a model can separate operator rules
    from the task prompt, and it never fails: a missing sheet is skipped.
    """
    if not skills_enabled():
        return ""
    parts = []
    for name in names:
        text = load_skill(name)
        if text:
            parts.append(f"### skill: {name}\n{text}")
    if not parts:
        return ""
    return HEADER + "\n\n".join(parts) + "\n"
