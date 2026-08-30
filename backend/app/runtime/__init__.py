"""Runtime / turns domain.

Owns session lifecycle, turn ordering, command acceptance, and the
transactional outbox that bridges API -> queue -> worker.

Currently stubbed; issues #187-#191 will implement durable primitives.
See `app/realtime` for projections and `app/observability` for tracing.
"""

