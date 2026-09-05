"""Meta (Llama API) chat-completions adapter (direct).

Meta's hosted Llama API is OpenAI-compatible:
  POST https://api.llama.com/v1/chat/completions
with ``Authorization: Bearer <key>``.

Accepts ``META_*`` env vars, with ``LLAMA_*`` as fallback aliases since
Meta's own docs/SDK default to ``LLAMA_API_KEY``.
"""

import os

from app.providers.adapters.base import LLMProviderAdapter


class MetaAdapter(LLMProviderAdapter):
    name = 'meta'
    env_prefix = 'META'
    default_base_url = 'https://api.llama.com/v1/chat/completions'
    default_model = 'Llama-4-Maverick-17B-128E-Instruct-FP8'

    def api_key(self):
        return os.environ.get('META_API_KEY', '') or os.environ.get('LLAMA_API_KEY', '')

    def env_model(self):
        return os.environ.get('META_MODEL', '') or os.environ.get('LLAMA_MODEL', '')

    def base_url(self, request=None):
        return (
            os.environ.get('META_BASE_URL')
            or os.environ.get('LLAMA_BASE_URL')
            or self.default_base_url
        )

    def require_config(self, model=None):
        if not self.api_key():
            raise RuntimeError('META_API_KEY (or LLAMA_API_KEY) is not set')
        if not (model if model is not None else self.env_model()):
            raise RuntimeError('META_MODEL (or LLAMA_MODEL) is not set')
