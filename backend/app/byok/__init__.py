"""BYOK (user provider credentials) — issue #257.

Secure user-managed credential records routed through the approved
generative/decision runtime. Secrets are encrypted at rest (server-side
Fernet key), never returned after storage, never logged, and never shared
with campaign members. Execution resolves role -> approved route exactly
like platform credentials; possession of a key is never approval by itself.
"""
