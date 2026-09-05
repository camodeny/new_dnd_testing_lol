"""OpenAI chat-completions adapter (direct)."""

from app.providers.adapters.base import LLMProviderAdapter


class OpenAIAdapter(LLMProviderAdapter):
    name = 'openai'
    env_prefix = 'OPENAI'
    default_base_url = 'https://api.openai.com/v1/chat/completions'
    default_model = 'gpt-4o-mini'
