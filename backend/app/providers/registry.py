"""Provider registry: name normalization and adapter lookup."""

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

    def names(self):
        return set(self._adapters)

    def get(self, name):
        normalized = self.normalize_name(name)
        adapter = self._adapters.get(normalized)
        if adapter is None:
            registered = ', '.join(sorted(self._adapters))
            raise RuntimeError(f"Unknown LLM provider {name!r} (registered: {registered})")
        return adapter


provider_registry = ProviderRegistry()
provider_registry.register(OpenRouterAdapter())
provider_registry.register(OpenAIAdapter())
provider_registry.register(MetaAdapter())
