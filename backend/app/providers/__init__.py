"""Providers — isolated LLM adapter surface.

Gameplay workflows must import provider abstractions from here (or
`llm_providers` directly) and use `provider_registry` / `stream_chat` /
`execute_chat` / `ProviderRequest`. They must not branch on provider names.

This module re-exports the registry so `app.*` code has a single seam to
mock in tests without importing transport.
"""
from llm_providers import (  # noqa: F401  re-export for app.* consumers
    ProviderCapabilities,
    ProviderError,
    ProviderRequest,
    NormalizedChatResponse,
    NormalizedStreamEvent,
    NormalizedToolCall,
    provider_registry,
    execute_chat,
    stream_chat,
)

__all__ = [
    "ProviderCapabilities",
    "ProviderError",
    "ProviderRequest",
    "NormalizedChatResponse",
    "NormalizedStreamEvent",
    "NormalizedToolCall",
    "provider_registry",
    "execute_chat",
    "stream_chat",
]

