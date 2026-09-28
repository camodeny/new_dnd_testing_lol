"""Credential-bound adapter proxy — issue #257.

Wraps a registered provider/decision adapter so one execution uses the
user's decrypted secret instead of the platform env key. All request
construction, capability declarations, response normalization, and error
classification delegate to the wrapped adapter — the only override is key
material. The plaintext secret lives only in this in-memory wrapper, is
never logged, and never leaves server-to-provider transport.
"""

from __future__ import annotations

from typing import Any


class ByokAdapterProxy:
    """Per-execution adapter bound to a user credential's secret."""

    def __init__(self, adapter: Any, secret: str) -> None:
        if not isinstance(secret, str) or not secret:
            raise ValueError("ByokAdapterProxy requires a non-empty secret")
        object.__setattr__(self, "_wrapped", adapter)
        object.__setattr__(self, "_secret", secret)

    def __repr__(self) -> str:
        try:
            name = self._wrapped.name
        except Exception:
            name = "?"
        return f"ByokAdapterProxy(adapter={name!r}, secret=<redacted>)"

    @property
    def name(self) -> str:
        return self._wrapped.name

    def api_key(self) -> str:
        return self._secret

    def build_headers(self) -> dict[str, str]:
        # Constructed directly (never by delegating to the wrapped
        # adapter): ``wrapped.build_headers()`` would bind ``self`` to the
        # wrapped adapter and reintroduce the platform env key. Both the
        # generative base adapter and the Jev adapter use this exact Bearer
        # shape; a future adapter with a different scheme must extend this
        # proxy explicitly.
        return {
            "Authorization": f"Bearer {self._secret}",
            "Content-Type": "application/json",
        }

    def require_config(self, model: str | None = None) -> None:
        if not self._secret:
            raise RuntimeError("BYOK credential secret is missing")
        # Validate the model side through the wrapped adapter without
        # requiring the platform env key to exist.
        try:
            wrapped_key = self._wrapped.api_key()
        except Exception:
            wrapped_key = ""
        if wrapped_key:
            self._wrapped.require_config(model)
            return
        # Platform key absent: replicate the model-presence gate only.
        model_value = model
        if model_value is None:
            try:
                model_value = self._wrapped.env_model()
            except Exception:
                model_value = None
        if not model_value:
            raise RuntimeError("model is not set for BYOK execution")

    def __getattr__(self, item: str) -> Any:
        return getattr(object.__getattribute__(self, "_wrapped"), item)

    def execute(self, request: Any, *, model: str, timeout: float) -> Any:
        """Decision-transport entry: run with the user secret bound.

        Decision adapters (``JevAdapter.execute``) call
        ``self.require_config()`` / ``self.build_headers()`` internally, so
        a ``__getattr__``-delegated bound method would run with
        ``self`` = the wrapped adapter and send the PLATFORM key (or fail
        on the platform key when it is absent). The per-execution shallow
        copy below rebinds only key material at the instance level, so all
        self-referential calls resolve to the user secret while the shared
        registered adapter is never mutated (thread-safe) and non-key
        behavior (URLs, models, capabilities, parsing) still delegates.
        """
        import copy

        target = copy.copy(self._wrapped)
        secret = self._secret
        target.api_key = lambda: secret  # type: ignore[method-assign]
        target.build_headers = lambda: {  # type: ignore[method-assign]
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        }
        return target.execute(request, model=model, timeout=timeout)


def wrap_generative_adapter(provider: str, secret: str) -> ByokAdapterProxy:
    """Wrap the registered generative adapter for ``provider``."""
    from app.providers.registry import provider_registry

    return ByokAdapterProxy(provider_registry.get(provider), secret)


def wrap_decision_adapter(provider: str, secret: str) -> ByokAdapterProxy:
    """Wrap the decision adapter for ``provider`` (``jev``/``typesafe``)."""
    from app.byok.routing import normalize_provider

    normalized = normalize_provider(provider)
    if normalized != "jev":
        from app.byok.errors import ByokError, UNSUPPORTED

        raise ByokError(
            f"provider {provider!r} has no decision adapter; "
            "decision-role BYOK requires an approved decision provider",
            kind=UNSUPPORTED,
        )
    from app.decisions.adapters.jev import JevAdapter

    return ByokAdapterProxy(JevAdapter(), secret)
