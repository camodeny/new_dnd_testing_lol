"""Providers — the single public surface for LLM adapters.

Gameplay workflows import provider abstractions from here and use
``provider_registry`` / ``stream_chat`` / ``execute_chat`` / ``ProviderRequest``.
They must not branch on provider names.

The implementation is split by responsibility:

- ``contracts``: normalized dataclasses and ``ProviderError``
- ``config``: environment-driven configuration and retry policy knobs
- ``adapters``: provider-specific request/response/stream handling
- ``transport``: chat/stream execution with transient retry and hooks
- ``registry``: adapter registration and name resolution
"""
from app.providers.adapters import (
    LLMProviderAdapter,
    OpenCodeGoAdapter,
    OpenRouterAdapter,
)
from app.providers.config import (
    default_max_attempts,
    retry_base_delay_seconds,
    retry_delay_seconds,
    retry_max_delay_seconds,
)
from app.providers.contracts import (
    NormalizedChatResponse,
    NormalizedStreamEvent,
    NormalizedToolCall,
    ProviderCapabilities,
    ProviderError,
    ProviderRequest,
)
from app.providers.registry import ProviderRegistry, provider_registry
from app.providers.transport import TransportHooks, execute_chat, stream_chat

__all__ = [
    "LLMProviderAdapter",
    "OpenRouterAdapter",
    "OpenCodeGoAdapter",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderRequest",
    "NormalizedChatResponse",
    "NormalizedStreamEvent",
    "NormalizedToolCall",
    "ProviderRegistry",
    "provider_registry",
    "TransportHooks",
    "execute_chat",
    "stream_chat",
    "default_max_attempts",
    "retry_base_delay_seconds",
    "retry_delay_seconds",
    "retry_max_delay_seconds",
]
