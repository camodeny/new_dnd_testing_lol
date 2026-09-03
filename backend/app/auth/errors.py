"""Typed auth errors — plain Python, no FastAPI.

``AuthError`` is the single failure type raised by the auth application layer
(``app.auth.jwt`` and ``app.auth.service``). It subclasses ``ValueError`` so
existing callers/tests that expected ``ValueError`` keep working, while still
giving the transport adapter a concrete type to map to HTTP responses.
"""


class AuthError(ValueError):
    """An authentication/authorization failure.

    ``message`` is a human-readable reason compatible with the historical
    ``HTTPException(detail=...)`` strings so API behavior is preserved.
    """
