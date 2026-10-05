"""Visibility vocabulary, normalization, and disclosure ordering.

Two vocabularies exist:

- Record visibility (world rows, scene, events, packets):
  ``public`` / ``campaign`` / ``private`` / ``dm_only``.
- Staged-effect visibility (DM turn contract effects):
  ``public`` / ``party_known`` / ``dm_private``.

The AI is the only DM. ``dm_only`` is DM-authority disclosure (the campaign
owner's lane); ``private`` discloses to exactly an explicit grantee set.
Every normalizer here fails closed: unknown spellings either raise or
collapse to the most restrictive value, never to a broader one.
"""

from __future__ import annotations

from typing import Any, Iterable

RECORD_VISIBILITIES = frozenset({"public", "campaign", "private", "dm_only"})
# Readable by any campaign participant.
MEMBER_VISIBILITIES = frozenset({"public", "campaign"})
# Never shown to an ordinary member without DM authority or an explicit grant.
RESTRICTED_VISIBILITIES = frozenset({"private", "dm_only"})

_RECORD_ALIASES = {"party": "campaign", "party_known": "campaign", "dm_private": "dm_only"}

# Openness lattice (higher = wider disclosure). ``dm_only`` is the most
# restrictive: it is adjudication-only, while ``private`` evidence can still
# be narration-eligible for its private audience.
DISCLOSURE_RANK = {"dm_only": 0, "private": 1, "campaign": 2, "public": 3}

EFFECT_VISIBILITIES = frozenset({"dm_private", "party_known", "public"})

# Record vocabulary onto the staged-effect broadening check: restricted stays
# restricted, member-visible stays member-visible.
_RECORD_TO_EFFECT = {"dm_only": "dm_private", "campaign": "party_known", "private": "dm_private"}

# Staged-effect vocabulary onto the record lattice. Effect ``public`` lands on
# ``campaign`` (campaign-wide, never world-public).
_EFFECT_TO_RECORD = {
    "public": "campaign", "party_known": "campaign", "dm_private": "dm_only",
    "campaign": "campaign", "private": "private", "dm_only": "dm_only",
}


def canonical_visibility(value: Any) -> str:
    """Strict record visibility. Missing or unknown values raise (callers deny)."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("visibility is required (fail closed)")
    canonical = _RECORD_ALIASES.get(raw, raw)
    if canonical not in RECORD_VISIBILITIES:
        raise ValueError(f"visibility must be one of {sorted(RECORD_VISIBILITIES)}")
    return canonical


def normalize_visibility(value: Any, *, default: str = "campaign") -> str:
    """Record visibility for writers: missing uses ``default``; unknown raises."""
    return canonical_visibility(str(value or default).strip() or default)


def visibility_or_dm_only(value: Any) -> str:
    """Lenient read: missing or unknown spellings collapse to ``dm_only``."""
    try:
        return canonical_visibility(value or "dm_only")
    except ValueError:
        return "dm_only"


def disclosure_rank(value: Any) -> int:
    """Rank on the openness lattice; unknown values raise."""
    return DISCLOSURE_RANK[canonical_visibility(value)]


def most_restrictive(*values: Any) -> str:
    """Narrowest disclosure among ``values`` (unknown counts as ``dm_only``)."""
    return min((visibility_or_dm_only(v) for v in values), key=DISCLOSURE_RANK.__getitem__)


def effect_to_record_visibility(value: Any) -> Any:
    """Staged-effect spelling onto record vocabulary; other values pass through."""
    return _EFFECT_TO_RECORD.get(str(value or "").strip(), value)


def record_to_effect_visibility(value: Any) -> str:
    return _RECORD_TO_EFFECT.get(str(value), str(value))


def world_event_visibility(record_visibility: Any) -> str:
    """Map a world-record disclosure level onto domain-event visibility.

    The campaign-events member-read path only returns ``public`` events to
    non-actors, while world reads treat both ``public`` and ``campaign``
    records as member-visible. Collapse both member-visible levels onto the
    event system's ``public`` value so ordinary members see history for
    records they can read; ``private``/``dm_only`` pass through unchanged
    and stay hidden from non-actors via the existing event filter.
    """
    normalized = normalize_visibility(record_visibility)
    if normalized in MEMBER_VISIBILITIES:
        return "public"
    return normalized


# Player-facing evidence bundles order audiences differently from the
# disclosure lattice: ``private`` outranks ``dm_only`` because a private
# bundle must carry its grantee ``user_ids`` authorization, and player-facing
# retrieval has already filtered what the viewer may not receive.
_PLAYER_BUNDLE_RANK = {"public": 0, "campaign": 1, "dm_only": 2, "private": 3}


def evidence_bundle_visibility(packet_visibilities: Iterable[Any], *, dm_internal: bool) -> str:
    """Visibility stamped on an evidence-tool result bundle."""
    visibilities = list(packet_visibilities)
    if dm_internal:
        if any(v in RESTRICTED_VISIBILITIES for v in visibilities):
            return "dm_only"
        return "campaign"
    bundle = "campaign"
    for visibility in visibilities:
        if _PLAYER_BUNDLE_RANK.get(visibility, 1) > _PLAYER_BUNDLE_RANK.get(bundle, 1):
            bundle = visibility
    return bundle
