"""OpenCode Go adapter.

Supports both the chat-completions surface and the OpenAI Responses surface
(selected per model family), with per-model thinking/reasoning-effort quirks.
"""
import json
import os

from app.providers.adapters.base import LLMProviderAdapter
from app.providers.config import _enabled
from app.providers.contracts import (
    NormalizedChatResponse,
    NormalizedStreamEvent,
    NormalizedToolCall,
    ProviderCapabilities,
    ProviderError,
)


class OpenCodeGoAdapter(LLMProviderAdapter):
    name = 'opencode_go'
    env_prefix = 'OPENCODE_GO'
    default_base_url = 'https://opencode.ai/zen/go/v1/chat/completions'
    responses_base_url = 'https://opencode.ai/zen/go/v1/responses'

    _THINKING_MODEL_FAMILIES = ('deepseek-v4-', 'mimo-', 'muse-spark-1.2')
    _RESPONSES_MODEL_FAMILIES = ('muse-spark-1.2', 'gpt-5.6-luna')

    @staticmethod
    def _model_name(model):
        return str(model or '').strip().lower()

    def uses_responses_api(self, model):
        return self._model_name(model).startswith(self._RESPONSES_MODEL_FAMILIES)

    def base_url(self, request=None):
        configured = os.environ.get(f'{self.env_prefix}_BASE_URL')
        if configured:
            return configured
        if request is not None and self.uses_responses_api(request.model):
            return self.responses_base_url
        return self.default_base_url

    def configured_reasoning_effort(self):
        return os.environ.get('OPENCODE_GO_REASONING_EFFORT', 'high')

    def capabilities_for(self, model):
        is_thinking_family = self._model_name(model).startswith(self._THINKING_MODEL_FAMILIES)
        thinking = is_thinking_family and _enabled(os.environ.get('OPENCODE_GO_THINKING', 'disabled'))
        effort = (os.environ.get('OPENCODE_GO_REASONING_EFFORT', 'high') or 'high').strip().lower()
        allowed_efforts = {'high', 'max'}
        if self.uses_responses_api(model):
            allowed_efforts.add('xhigh')
        if effort not in allowed_efforts:
            effort = 'high'
        return ProviderCapabilities(
            supports_tool_choice_required=not is_thinking_family,
            supports_parallel_tool_calls=True,
            supports_thinking=thinking,
            reasoning_effort=effort if thinking else None,
        )

    def supports_thinking_for_model(self, model):
        return self._model_name(model).startswith(self._THINKING_MODEL_FAMILIES)

    @staticmethod
    def _responses_input(messages):
        """Translate the app's chat transcript into OpenAI Responses input items."""
        items = []
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            role = str(message.get('role') or 'user').strip().lower()
            content = message.get('content') or ''
            if role == 'tool':
                items.append({
                    'type': 'function_call_output',
                    'call_id': message.get('tool_call_id'),
                    'output': content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                })
                continue
            if role == 'assistant':
                if content:
                    items.append({
                        'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': str(content)}],
                    })
                for call in message.get('tool_calls') or []:
                    function = call.get('function') or {} if isinstance(call, dict) else {}
                    items.append({
                        'type': 'function_call',
                        'call_id': call.get('id') if isinstance(call, dict) else None,
                        'name': function.get('name'),
                        'arguments': function.get('arguments') or '{}',
                    })
                continue
            # The Responses API treats system instructions as developer input.
            normalized_role = 'developer' if role == 'system' else 'user'
            items.append({
                'role': normalized_role,
                'content': [{'type': 'input_text', 'text': str(content)}],
            })
        return items

    @staticmethod
    def _responses_tools(tools):
        translated = []
        for tool in tools or []:
            function = tool.get('function') or {} if isinstance(tool, dict) else {}
            if function:
                translated.append({
                    'type': 'function',
                    'name': function.get('name'),
                    'description': function.get('description', ''),
                    'parameters': function.get('parameters') or {'type': 'object', 'properties': {}},
                })
        return translated

    def build_payload(self, request):
        if not self.uses_responses_api(request.model):
            return super().build_payload(request)
        capabilities = self.capabilities_for(request.model)
        if request.json_mode and not capabilities.supports_json_mode:
            raise ProviderError(
                f'Provider {self.name} does not support JSON mode for model {request.model!r}',
                provider=self.name, kind='unsupported_feature',
            )
        options = self.payload_options(
            request.model,
            request.tools,
            request.tool_choice,
            request.parallel_tool_calls,
            allow_thinking=request.allow_thinking,
            force_thinking=request.force_thinking,
            force_tool_choice=request.force_tool_choice,
        )
        payload = {'model': request.model, 'input': self._responses_input(request.messages)}
        if request.max_tokens is not None:
            payload['max_output_tokens'] = request.max_tokens
        if request.tools:
            payload['tools'] = self._responses_tools(request.tools)
        if options.get('tool_choice') is not None:
            payload['tool_choice'] = options['tool_choice']
        if options.get('parallel_tool_calls') is not None and capabilities.supports_parallel_tool_calls:
            payload['parallel_tool_calls'] = options['parallel_tool_calls']
        if request.reasoning_effort:
            effort = str(request.reasoning_effort).strip().lower()
            allowed_efforts = {'minimal', 'low', 'medium', 'high', 'xhigh'}
            if self._model_name(request.model).startswith('gpt-5.6-luna'):
                allowed_efforts.add('none')
            if effort not in allowed_efforts:
                raise ProviderError(
                    f'Unsupported Responses reasoning effort: {request.reasoning_effort!r}',
                    provider=self.name,
                    kind='unsupported_feature',
                )
            payload['reasoning'] = {'effort': effort}
        elif options.get('thinking_enabled'):
            payload['reasoning'] = {'effort': options['reasoning_effort']}
        if request.json_schema:
            payload['text'] = {
                'format': {
                    'type': 'json_schema',
                    'name': request.json_schema_name or 'structured_response',
                    'strict': True,
                    'schema': request.json_schema,
                },
            }
        elif request.json_mode:
            payload['text'] = {'format': {'type': 'json_object'}}
        if request.stream:
            payload['stream'] = True
        return payload

    def parse_response(self, data):
        if not isinstance(data, dict) or 'output' not in data:
            return super().parse_response(data)
        output = data.get('output') or []
        if not isinstance(output, list):
            raise ProviderError(
                f'Provider {self.name} returned a response without output items',
                provider=self.name, kind='malformed',
            )
        text_parts = []
        tool_calls = []
        reasoning_parts = []
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get('type')
            if item_type == 'function_call':
                tool_calls.append(NormalizedToolCall(
                    id=item.get('call_id') or item.get('id'),
                    name=item.get('name'),
                    arguments=item.get('arguments') or '{}',
                    raw=item,
                ))
            elif item_type == 'reasoning':
                summary = item.get('summary') or []
                for part in summary if isinstance(summary, list) else []:
                    if isinstance(part, dict) and isinstance(part.get('text'), str):
                        reasoning_parts.append(part['text'])
            else:
                for part in item.get('content') or []:
                    if isinstance(part, dict) and part.get('type') in {'output_text', 'text'}:
                        if isinstance(part.get('text'), str):
                            text_parts.append(part['text'])
        return NormalizedChatResponse(
            provider=self.name,
            model=data.get('model'),
            content=''.join(text_parts) or str(data.get('output_text') or ''),
            tool_calls=tool_calls,
            finish_reason=data.get('status') or (data.get('incomplete_details') or {}).get('reason'),
            usage=data.get('usage') or {},
            reasoning=''.join(reasoning_parts) or None,
            reasoning_details=None,
            raw=data,
        )

    def iter_stream_events(self, response):
        tool_calls: dict[str, dict] = {}
        current_call_id: str | None = None
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith('data: '):
                continue
            data_str = line[6:]
            if data_str.strip() == '[DONE]':
                break
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            t = event.get('type')
            if t == 'response.output_text.delta' and event.get('delta'):
                yield NormalizedStreamEvent(kind='token', text=str(event['delta']))
            elif t == 'response.output_item.added':
                item = event.get('item') or {}
                if item.get('type') == 'function_call':
                    cid = str(item.get('call_id') or item.get('id') or len(tool_calls))
                    tool_calls[cid] = {"id": cid, "name": item.get('name'), "arguments": item.get('arguments') or ""}
                    current_call_id = cid
            elif t == 'response.function_call_arguments.delta':
                delta = event.get('delta') or ""
                cid = str(event.get('item_id') or event.get('call_id') or current_call_id or len(tool_calls))
                if cid in tool_calls:
                    tool_calls[cid]["arguments"] += str(delta)
                elif current_call_id and current_call_id in tool_calls:
                    tool_calls[current_call_id]["arguments"] += str(delta)
                else:
                    tool_calls[cid] = {"id": cid, "name": None, "arguments": str(delta)}
                    current_call_id = cid
            elif t == 'response.output_item.done':
                item = event.get('item') or {}
                if item.get('type') == 'function_call':
                    cid = str(item.get('call_id') or item.get('id') or current_call_id or "")
                    if cid and cid in tool_calls:
                        if item.get('name'):
                            tool_calls[cid]["name"] = item.get('name')
                        if item.get('arguments'):
                            tool_calls[cid]["arguments"] = item.get('arguments')
                    elif item.get('name'):
                        cid = str(item.get('call_id') or item.get('id') or len(tool_calls))
                        tool_calls[cid] = {"id": cid, "name": item.get('name'), "arguments": item.get('arguments') or ""}
            elif t == 'response.completed':
                # some providers bundle final output with function calls
                resp = event.get('response') or {}
                for item in resp.get('output') or []:
                    if isinstance(item, dict) and item.get('type') == 'function_call':
                        cid = str(item.get('call_id') or item.get('id') or len(tool_calls))
                        if cid not in tool_calls:
                            tool_calls[cid] = {"id": cid, "name": item.get('name'), "arguments": item.get('arguments') or ""}
        for tc in tool_calls.values():
            yield NormalizedStreamEvent(
                kind='tool_call',
                tool_call=NormalizedToolCall(id=tc.get('id'), name=tc.get('name'), arguments=tc.get('arguments') or "", raw=tc),
            )
        yield NormalizedStreamEvent(kind='done')
