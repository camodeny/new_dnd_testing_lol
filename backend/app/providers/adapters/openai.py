"""OpenAI chat-completions adapter (direct)."""

from app.providers.adapters.base import LLMProviderAdapter


def strict_schema_for_openai(node):
    """OpenAI strict structured outputs reject keywords beside ``$ref``.

    Pydantic emits ``{"$ref": ..., "description": ...}`` for documented
    model-typed fields; OpenAI 400s on those siblings, so each ``$ref`` node
    keeps only the reference (the referenced definition keeps its own
    description).
    """
    if isinstance(node, dict):
        if '$ref' in node:
            return {'$ref': node['$ref']}
        return {key: strict_schema_for_openai(value) for key, value in node.items()}
    if isinstance(node, list):
        return [strict_schema_for_openai(value) for value in node]
    return node


class OpenAIAdapter(LLMProviderAdapter):
    name = 'openai'
    env_prefix = 'OPENAI'
    default_base_url = 'https://api.openai.com/v1/chat/completions'
    default_model = 'gpt-4o-mini'

    def build_payload(self, request):
        payload = super().build_payload(request)
        response_format = payload.get('response_format') or {}
        if 'json_schema' in response_format:
            response_format['json_schema']['schema'] = strict_schema_for_openai(
                response_format['json_schema']['schema']
            )
        if payload.get('reasoning_effort') not in (None, 'none'):
            # Reasoning models accept only the default temperature once they
            # reason; an explicit one is a 400.
            payload.pop('temperature', None)
        return payload
