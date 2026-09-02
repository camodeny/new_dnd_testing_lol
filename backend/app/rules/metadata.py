"""Corpus provenance / licensing metadata — issue #223."""

from __future__ import annotations

OFFICIAL_SRD_URL = "https://www.dndbeyond.com/srd"
OFFICIAL_SRD_URL_ALT = "https://media.dndbeyond.com/compendium-images/srd/SRD_CC_v5.2.1.pdf"
LICENSE = "CC BY 4.0"
ATTRIBUTION = (
    "This work includes material taken from the System Reference Document 5.2.1 (SRD 5.2.1) "
    "by Wizards of the Coast LLC and available at https://www.dndbeyond.com/srd. "
    "The SRD 5.2.1 is licensed under the Creative Commons Attribution 4.0 International License "
    "available at https://creativecommons.org/licenses/by/4.0/legalcode."
)
CORPUS_ID = "dnd-srd"
CORPUS_VERSION = "5.2.1"

# Pinned structured inputs — treat as derivative, validate against authority before promotion
PINNED_CANTILUX_COMMIT = "main"  # override via env CANTILUX_COMMIT
PINNED_MARKDOWN_COMMIT = "main"
OPEN5E_REF = "main"

# Pinned trusted checksum of official WotC artifact bytes for verification.
# This MUST be the hash of the authoritative source artifact (e.g. SRD_CC_v5.2.1.pdf bytes),
# not the derivative dataset hash. Promotion will reject mismatches for pinned versions.
# To compute for production: curl -sL -H "Referer: https://www.dndbeyond.com/" https://media.dndbeyond.com/compendium-images/srd/SRD_CC_v5.2.1.pdf | shasum -a 256
# For CI/tests we pin a deterministic dummy; replace with real hash before prod promotion.
PINNED_OFFICIAL_ARTIFACT_HASH = "a" * 64
PINNED_OFFICIAL_ARTIFACT_HASHES: dict[str, str] = {
    CORPUS_VERSION: PINNED_OFFICIAL_ARTIFACT_HASH,
    # Add future pinned versions here, e.g. "5.2.2": "<sha256>"
}

# Known 5.2.1 canary: must be present to prove 5.2.1 not stale 5.2
CANARY_RULES = [
    {"needle": "weapon mastery", "reason": "5.2.1 Weapon Mastery addition"},
]
