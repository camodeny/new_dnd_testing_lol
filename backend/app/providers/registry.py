"""Provider registry: name normalization and adapter lookup."""

import os

from app.providers.adapters import MetaAdapter, OpenAIAdapter, OpenRouterAdapter


class ProviderRegistry:
    def __init__(self):
        self._adapters = {}

    @staticmethod
    def normalize_name(value):
        return (value or '').strip().lower().replace('-', '_')

    def register(self, adapter):
        self._adapters[adapter.name] = adapter
        return adapter

    def unregister(self, name):
        self._adapters.pop(self.normalize_name(name), None)

    def names(self):
        return set(self._adapters)

    def get(self, name):
        normalized = self.normalize_name(name)
        adapter = self._adapters.get(normalized)
        if adapter is None:
            registered = ', '.join(sorted(self._adapters))
            raise RuntimeError(f"Unknown LLM provider {name!r} (registered: {registered})")
        return adapter

    def current(self):
        return self.get(os.environ.get('LLM_PROVIDER', 'openrouter') or 'openrouter')

    def _lenient_current_name(self):
        normalized = self.normalize_name(os.environ.get('LLM_PROVIDER', 'openrouter') or 'openrouter')
        return normalized if normalized in self._adapters else 'openrouter'

    def resolve_provider_and_model(self, model=None):
        """Resolve an optional 'provider/model' hint to (provider_name, model)."""
        raw_model = (model or '').strip()
        if '/' in raw_model:
            provider_hint, model_id = raw_model.split('/', 1)
            normalized = self.normalize_name(provider_hint)
            if normalized in self._adapters:
                return normalized, model_id
            provider = self._lenient_current_name()
            return provider, self.get(provider).env_model()
        provider = self._lenient_current_name()
        return provider, raw_model or self.get(provider).env_model()


provider_registry = ProviderRegistry()
provider_registry.register(OpenRouterAdapter())
provider_registry.register(OpenAIAdapter())
provider_registry.register(MetaAdapter())
