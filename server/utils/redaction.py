import copy

EXACT_SENSITIVE_KEYS = {
    'token',
    'lease_token',
    'access_token',
    'refresh_token',
    'authorization',
    'api_key',
    'password',
    'secret'
}

SUFFIX_SENSITIVE_KEYS = (
    '_token',
    '_secret',
    '_api_key',
    '_password'
)

PREFIX_SENSITIVE_KEYS = (
    'token_',
    'secret_'
)

EXCLUDED_KEYS = {
    'usage_input_tokens',
    'usage_output_tokens',
    'usage_total_tokens',
    'token_count'
}

def is_sensitive_key(key: str) -> bool:
    if not isinstance(key, str):
        return False
    k_lower = key.lower()
    if k_lower in EXCLUDED_KEYS:
        return False
    if k_lower in EXACT_SENSITIVE_KEYS:
        return True
    if k_lower.startswith(PREFIX_SENSITIVE_KEYS):
        return True
    if k_lower.endswith(SUFFIX_SENSITIVE_KEYS):
        return True
    return False

def redact_secrets(data):
    """
    Recursively redacts secrets in dicts, lists, and nested objects.
    Returns a copy/new structure where sensitive keys are replaced with "[REDACTED]".
    """
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if is_sensitive_key(key):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact_secrets(value)
        return result
    elif isinstance(data, list):
        return [redact_secrets(item) for item in data]
    return data
