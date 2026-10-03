"""Deterministic stable ID derivation for rules corpus — issue #223.

Must not let embedding IDs, DB UUIDs, or third-party slugs become public identity.

- source_section_id / source_locator: deterministic kebab from full document + heading hierarchy
- rule_id: namespaced application-owned identity (corpus prefix + source_section_id dotted)
- collision detection fails closed, requires alias manifest.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

# Default alias manifest location (checked-in)
ALIASES_PATH = Path(__file__).with_name("aliases.json")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SLUG_TRIM = re.compile(r"^-+|-+$")
_MAX_SLUG = 80


def slugify(text: str) -> str:
    """Deterministic kebab slug: unicode fold, lower, non-alphanum -> '-', trimmed."""
    if not text:
        return "untitled"
    # NFKD fold, strip accents
    norm = unicodedata.normalize("NFKD", text)
    ascii_only = norm.encode("ascii", "ignore").decode("ascii")
    lower = ascii_only.lower().strip()
    kebab = _SLUG_RE.sub("-", lower)
    kebab = _SLUG_TRIM.sub("", kebab)
    if not kebab:
        # fallback to hash fragment so collision detection still works
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        return f"section-{h}"
    if len(kebab) > _MAX_SLUG:
        # keep prefix + hash to preserve determinism without arbitrary truncation collisions
        prefix = kebab[: _MAX_SLUG - 9]
        prefix = _SLUG_TRIM.sub("", prefix)
        h = hashlib.sha256(kebab.encode("utf-8")).hexdigest()[:8]
        kebab = f"{prefix}-{h}"
    return kebab


def derive_rule_id_with_path(corpus_id: str, corpus_version: str, document: str, heading_path: list[str]) -> tuple[str, str]:
    """Precise rule_id using document+heading segmentation (not just flattened kebab).

    Returns (rule_id, source_section_id).
    Example heading_path ["Combat","Melee Attacks"] -> source "playing-the-game-combat-melee-attacks"
    -> rule_id "srd521.playing-the-game.combat.melee-attacks"
    """
    slugs: list[str] = []
    doc_slug = slugify(document)
    if doc_slug:
        slugs.append(doc_slug)
    for h in heading_path or []:
        if h and h.strip():
            slugs.append(slugify(h))
    source_section_id = "-".join(slugs) if slugs else "untitled"
    prefix = corpus_id.lower().replace("-", "")
    ver_digits = re.sub(r"[^0-9]", "", corpus_version)
    if not ver_digits:
        ver_digits = "0"
    ns = f"{prefix}{ver_digits}"
    dotted = ".".join(slugs) if slugs else "untitled"
    return f"{ns}.{dotted}", source_section_id


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_aliases(path: Path | None = None) -> dict[str, str]:
    p = path or ALIASES_PATH
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        return {}
    except Exception:
        return {}


def check_collisions(records: list[dict], *, aliases: dict[str, str] | None = None) -> None:
    """Fail closed on unresolved source_section_id or rule_id collisions.

    records: list of dicts with keys rule_id, source_section_id
    aliases: explicit override manifest; collisions resolved by alias are allowed.
    Raises ValueError with collision details.
    """
    seen_source: dict[str, str] = {}
    seen_rule: dict[str, str] = {}
    for rec in records:
        src = rec.get("source_section_id")
        rid = rec.get("rule_id")
        title = rec.get("title", "?")
        # Duplicate source or rule is a collision unless alias resolves it
        if src and src in seen_source:
            if not (aliases and (src in aliases or (rid and rid in aliases))):
                raise ValueError(
                    f"Collision: source_section_id {src!r} appears multiple times "
                    f"({seen_source[src]!r} vs {rid!r} title {title!r}). "
                    f"Add explicit alias to backend/app/rules_corpus/aliases.json"
                )
        if rid and rid in seen_rule:
            if not (aliases and rid in aliases):
                raise ValueError(
                    f"Collision: rule_id {rid!r} appears multiple times "
                    f"({seen_rule[rid]!r} vs {src!r}). "
                    f"Add explicit alias to backend/app/rules_corpus/aliases.json"
                )
        # Also detect same source mapping to different rule (covers corpus version drift)
        if src in seen_source and seen_source[src] != rid:
            if not (aliases and (src in aliases or rid in aliases)):
                raise ValueError(
                    f"Collision: source_section_id {src!r} maps to multiple rule_ids "
                    f"{seen_source[src]!r} vs {rid!r} (title {title!r}). "
                    f"Add explicit alias to backend/app/rules_corpus/aliases.json"
                )
        if rid in seen_rule and seen_rule[rid] != src:
            if not (aliases and rid in aliases):
                raise ValueError(
                    f"Collision: rule_id {rid!r} maps to multiple source_section_ids "
                    f"{seen_rule[rid]!r} vs {src!r}. "
                    f"Add explicit alias to backend/app/rules_corpus/aliases.json"
                )
        if src:
            seen_source[src] = rid
        if rid:
            seen_rule[rid] = src
