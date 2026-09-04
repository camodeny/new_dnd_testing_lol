"""Domain models package — one module per domain, all sharing ``database.Base``.

Importing this package registers every table on ``Base.metadata`` (Alembic
depends on that side effect). Model classes live in their domain modules and
should be imported directly from there, e.g. ``from models.campaigns import
Campaign``. This module intentionally does not re-export the classes.
"""

from . import campaigns, characters, dm, profiles, reliability, rules, threads, world

__all__ = [
    "campaigns",
    "characters",
    "dm",
    "profiles",
    "reliability",
    "rules",
    "threads",
    "world",
]
