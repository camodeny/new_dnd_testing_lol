"""Meta Model API chat-completions adapter (direct).

Muse models (Muse Spark) are served from Meta's Model API, which is
OpenAI-compatible: ``POST https://api.meta.ai/v1/chat/completions``
with ``Authorization: Bearer <MODEL_API_KEY>`` and standard
``choices[0].message`` / ``choices[].delta`` envelopes. The shared base
normalizer therefore handles responses; this adapter only declares the
endpoint and credentials.

Accepts ``MODEL_API_KEY`` (Meta's documented variable) with
``META_API_KEY`` / ``LLAMA_API_KEY`` as compatibility aliases.
"""

import os

from app.providers.adapters.base import LLMProviderAdapter


class MetaAdapter(LLMProviderAdapter):
    name = 'meta'
    env_prefix = 'META'
    default_base_url = 'https://api.meta.ai/v1/chat/completions'
    default_model = 'muse-spark-1.3'

    def api_key(self):
        return (
            os.environ.get('MODEL_API_KEY', '')
            or os.environ.get('META_API_KEY', '')
            or os.environ.get('LLAMA_API_KEY', '')
        )

    def require_config(self, model=None):
        if not self.api_key():
            raise RuntimeError('MODEL_API_KEY (or META_API_KEY) is not set')
        if not (model if model is not None else self.env_model()):
            raise RuntimeError('META_MODEL is not set')
