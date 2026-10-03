"""Deterministic 5e game mechanics: sheet-derived stats, checks/saves,
attacks/damage, spells, and rules-state effects.

No FastAPI or provider code. Resolution operates on value objects and
returns results for the application layer to persist; the only database
read is ``get_character_mechanics`` loading the canonical sheet. The SRD
rules-text corpus and its search live in ``app.rules_corpus``.
"""
