"""Domain models package — one module per domain, all sharing ``database.Base``.

Importing this package registers every table on ``Base.metadata`` (Alembic
depends on that side effect). Model classes live in their domain modules and
should be imported directly from there, e.g. ``from models.campaigns import
Campaign``. This module intentionally does not re-export the classes.
"""

from . import byok, campaigns, characters, combat, dm, funding, post_turn, profiles, reliability, repair, rules, threads, usage, world

__all__ = [
    "byok",
    "campaigns",
    "characters",
    "combat",
    "dm",
    "funding",
    "post_turn",
    "profiles",
    "reliability",
    "repair",
    "rules",
    "threads",
    "usage",
    "world",
]
