"""Infrastructure — shared Postgres / database layer.

The authoritative store is Postgres (Supabase). `database.py` exposes
`engine`, `SessionLocal`, `get_db`, `Base`, and `get_database_url`.
The `models` package defines domain aggregates as SQLAlchemy tables.
There is no per-module database; all modules share this layer.
"""

