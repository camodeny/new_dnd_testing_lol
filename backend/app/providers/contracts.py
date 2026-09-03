"""Provider-neutral contracts shared by adapters and transport.

These dataclasses form the normalized surface gameplay workflows consume;
adapters translate provider-native payloads into them, and transport returns
them. Nothing here depends on ``requests`` or any specific provider.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declared feature support for one (provider, model) pair.

    Workflows must gate optional behavior on these declarations instead of
    checking provider or model names.
    """
    supports_json_mode: bool = True
    supports_tool_choice_required: bool = True
    supports_parallel_tool_calls: bool = True
    supports_thinking: bool = False
    reasoning_effort: Optional[str] = None


@dataclass
class ProviderRequest:
    messages: list
    model: str
    json_mode: bool = False
    json_schema: Optional[dict] = None
    json_schema_name: Optional[str] = None
    reasoning_effort: Optional[str] = None
    tools: Optional[list] = None
    tool_choice: Optional[object] = None
    parallel_tool_calls: Optional[bool] = None
    allow_thinking: bool = True
    force_thinking: bool = False
    force_tool_choice: bool = False
    timeout_seconds: float = 60
    max_attempts: Optional[int] = None
    max_tokens: Optional[int] = None
    stream: bool = False


@dataclass
class NormalizedToolCall:
    id: Optional[str]
    name: Optional[str]
    arguments: object
    raw: dict = field(default_factory=dict)


@dataclass
class NormalizedChatResponse:
    provider: str
    model: Optional[str]
    content: str
    tool_calls: list
    finish_reason: Optional[str]
    usage: dict
    reasoning: Optional[str]
    reasoning_details: object
    raw: dict

    def message_view(self):
        """Canonical assistant-message payload for workflow consumption.

        Built solely from normalized fields, so workflows never need to index
        the provider's native response shape. ``raw`` remains available for
        audit/debugging only.
        """
        message = {'content': self.content}
        if self.tool_calls:
            message['tool_calls'] = [
                {
                    'id': tool_call.id,
                    'type': 'function',
                    'function': {'name': tool_call.name, 'arguments': tool_call.arguments},
                }
                for tool_call in self.tool_calls
            ]
        if self.reasoning:
            message['reasoning_content'] = self.reasoning
        if self.reasoning_details is not None:
            message['reasoning_details'] = self.reasoning_details
        return message


@dataclass
class NormalizedStreamEvent:
    kind: str  # 'token' | 'tool_call' | 'done'
    text: str = ''
    tool_call: Optional[NormalizedToolCall] = None


class ProviderError(Exception):
    """Normalized provider failure.

    ``retryable`` distinguishes transient transport failures from permanent
    ones; ``kind`` is one of 'http', 'connection', 'timeout', 'malformed',
    'config', 'unsupported_feature'.
    """

    def __init__(self, message, *, provider=None, status_code=None, retryable=False, kind='http', original=None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.kind = kind
        self.original = original
