#!/usr/bin/env python3
import base64
import json
import os
import pathlib
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / 'automation' / 'state'


class ApiError(RuntimeError):
    pass


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def json_dump(path, payload):
    ensure_state_dir()
    pathlib.Path(path).write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')


def json_load(path):
    return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))


def auth_headers(owner_token=None, api_key=None, extra=None):
    headers = {'Content-Type': 'application/json'}
    if owner_token:
        headers['Authorization'] = f'Bearer {owner_token}'
    if api_key:
        headers['X-API-Key'] = api_key
    if extra:
        headers.update(extra)
    return headers


def default_api_timeout():
    try:
        return int(os.environ.get('DND_API_TIMEOUT', '30'))
    except (TypeError, ValueError):
        return 30


def default_session_start_timeout():
    try:
        return int(os.environ.get('DND_SESSION_START_TIMEOUT', '180'))
    except (TypeError, ValueError):
        return 180


def request_json(url, method='GET', headers=None, payload=None, timeout=30):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode('utf-8')
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        details = exc.read().decode('utf-8', errors='replace')
        raise ApiError(f'{method} {url} -> HTTP {exc.code}: {details}') from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ApiError(f'{method} {url} timed out after {timeout}s') from exc
    except urllib.error.URLError as exc:
        raise ApiError(f'{method} {url} failed: {exc.reason}') from exc


def api_url(base, path, query=None):
    query_string = ''
    if query:
        query_string = '?' + urllib.parse.urlencode(query)
    return f'{base.rstrip("/")}{path}{query_string}'


def api_get(base, path, owner_token=None, api_key=None, query=None):
    return request_json(
        api_url(base, path, query=query),
        headers=auth_headers(owner_token=owner_token, api_key=api_key),
        timeout=default_api_timeout(),
    )


def api_post(base, path, payload, owner_token=None, api_key=None, timeout=None):
    return request_json(
        api_url(base, path),
        method='POST',
        headers=auth_headers(owner_token=owner_token, api_key=api_key),
        payload=payload,
        timeout=timeout if timeout is not None else default_api_timeout(),
    )


def api_put(base, path, payload, owner_token=None):
    return request_json(
        api_url(base, path),
        method='PUT',
        headers=auth_headers(owner_token=owner_token),
        payload=payload,
        timeout=default_api_timeout(),
    )


def api_put_with_key(base, path, payload, api_key=None):
    return request_json(
        api_url(base, path),
        method='PUT',
        headers=auth_headers(api_key=api_key),
        payload=payload,
        timeout=default_api_timeout(),
    )


def find_active_session(base, campaign_id, owner_token=None, api_key=None):
    sessions = api_get(
        base,
        f'/api/campaigns/{campaign_id}/sessions',
        owner_token=owner_token,
        api_key=api_key,
    ).get('sessions') or []
    for session in sessions:
        if session.get('is_active'):
            return session
    return None


def start_session(base, campaign_id, owner_token=None, api_key=None, timeout=None, poll_interval=5):
    timeout = timeout if timeout is not None else default_session_start_timeout()
    active_session = find_active_session(
        base,
        campaign_id,
        owner_token=owner_token,
        api_key=api_key,
    )
    if active_session:
        return active_session

    try:
        return api_post(
            base,
            f'/api/campaigns/{campaign_id}/sessions',
            {},
            owner_token=owner_token,
            api_key=api_key,
            timeout=timeout,
        )['session']
    except ApiError as exc:
        if 'timed out' not in str(exc):
            raise

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active_session = find_active_session(
            base,
            campaign_id,
            owner_token=owner_token,
            api_key=api_key,
        )
        if active_session:
            return active_session
        time.sleep(poll_interval)

    raise RuntimeError(
        f'Session start request timed out and no active session appeared within {timeout}s. '
        'The server may be overloaded or stuck in opening-scene generation.'
    )


def load_manifest(path):
    return json_load(path)


def save_manifest(path, payload):
    json_dump(path, payload)


def extract_text_parts(parts):
    text_chunks = []
    for part in parts or []:
        if isinstance(part, str):
            text_chunks.append(part)
            continue
        if not isinstance(part, dict):
            continue
        for key in ('text', 'content'):
            value = part.get(key)
            if isinstance(value, str) and value.strip():
                text_chunks.append(value)
                break
    return '\n'.join(chunk.strip() for chunk in text_chunks if chunk and chunk.strip()).strip()


def opencode_headers(password=None):
    headers = {'Content-Type': 'application/json'}
    if password:
        token = base64.b64encode(f'opencode:{password}'.encode('utf-8')).decode('ascii')
        headers['Authorization'] = f'Basic {token}'
    return headers


def opencode_request(server, path, payload=None, password=None, method='GET'):
    return request_json(
        f'{server.rstrip("/")}{path}',
        method=method,
        headers=opencode_headers(password=password),
        payload=payload,
        timeout=120,
    )


def default_api_base():
    return os.environ.get('DND_API_BASE', 'http://127.0.0.1:5889')


def default_opencode_server():
    return os.environ.get('OPENCODE_SERVER', 'http://127.0.0.1:4096')
