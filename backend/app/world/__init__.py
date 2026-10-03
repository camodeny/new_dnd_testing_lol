"""World / memory / knowledge domain.

Owns authoritative campaign world state: canonical world entities, the
current-scene row (issue #209), facts/relations, knowledge, clocks, and NPC
state. Writers run inside the caller's turn or post-turn transaction.
"""
