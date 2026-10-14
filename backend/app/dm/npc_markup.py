"""NPC dialogue markup in DM narration text.

Narration wraps each NPC line as ``<npc speaker="Name">"utterance"</npc>``
so clients can render dialogue as its own block with the speaker attached.
This module owns the format: rendering it (deterministic narrator), parsing
it (fidelity gate), and flattening it for plain-text consumers.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Iterator

NPC_LINE_RE = re.compile(r"<npc\b([^>]*)>(.*?)</npc\s*>", re.DOTALL | re.IGNORECASE)
_SPEAKER_ATTR_RE = re.compile(r"""\bspeaker\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_STRAY_TAG_RE = re.compile(r"</?npc\b[^>]*>", re.IGNORECASE)


@dataclass(frozen=True)
class NpcLine:
    speaker: str
    text: str


def render_npc_line(speaker: str, utterance: str) -> str:
    """One NPC line in the narration markup, utterance in straight quotes."""
    name = html.escape(speaker.strip(), quote=True)
    return f'<npc speaker="{name}">"{utterance.strip()}"</npc>'


def iter_npc_lines(text: str) -> Iterator[NpcLine]:
    """Yield every complete ``<npc>`` line in ``text`` (speaker may be empty)."""
    for match in NPC_LINE_RE.finditer(text or ""):
        attr = _SPEAKER_ATTR_RE.search(match.group(1))
        speaker = html.unescape(next((g for g in attr.groups() if g is not None), "")) if attr else ""
        yield NpcLine(speaker=speaker.strip(), text=match.group(2).strip())


def strip_npc_markup(text: str) -> str:
    """Flatten NPC markup to ``Speaker: "utterance"`` for plain-text consumers."""

    def _flat(match: re.Match[str]) -> str:
        line = next(iter_npc_lines(match.group(0)))
        return f"{line.speaker}: {line.text}" if line.speaker else line.text

    return _STRAY_TAG_RE.sub("", NPC_LINE_RE.sub(_flat, text or ""))
