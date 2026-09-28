"""Deterministic BYOK failure taxonomy — issue #257 (code-owned).

Kinds map to HTTP status in the router; gameplay/runtime code branches on
kind, never on message text. Messages never include secret material.
"""

from __future__ import annotations


class ByokError(Exception):
    """Normalized BYOK failure with a machine-readable kind."""

    def __init__(self, message: str, *, kind: str = "malformed") -> None:
        super().__init__(message)
        self.kind = kind


# Client correctable: bad provider/secret/label/model/role shape.
MALFORMED = "malformed"
# Caller may not manage this credential or policy.
FORBIDDEN = "forbidden"
# Unknown id (also used for cross-owner reads: no existence leak).
NOT_FOUND = "not_found"
# Provider/model/role/adapter combination is not approved for BYOK.
UNSUPPORTED = "unsupported"
# Credential rejected by its provider (bad/expired key).
INVALID_CREDENTIAL = "invalid_credential"
# Provider unreachable during test-connection (retryable upstream).
UPSTREAM_UNREACHABLE = "upstream_unreachable"
# Server misconfigured (e.g. missing BYOK_ENCRYPTION_KEY).
SERVER_MISCONFIGURED = "server_misconfigured"


def http_status_for(kind: str) -> int:
    return {
        MALFORMED: 400,
        FORBIDDEN: 403,
        NOT_FOUND: 404,
        UNSUPPORTED: 422,
        INVALID_CREDENTIAL: 502,
        UPSTREAM_UNREACHABLE: 502,
        SERVER_MISCONFIGURED: 500,
    }.get(kind, 400)
