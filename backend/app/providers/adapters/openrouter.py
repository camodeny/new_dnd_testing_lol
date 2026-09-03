"""OpenRouter chat-completions adapter."""

from app.providers.adapters.base import LLMProviderAdapter


class OpenRouterAdapter(LLMProviderAdapter):
    name = 'openrouter'
    env_prefix = 'OPENROUTER'
    default_base_url = 'https://openrouter.ai/api/v1/chat/completions'
