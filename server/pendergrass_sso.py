from urllib.parse import urlencode, urljoin

import requests


class PendergrassSSOError(RuntimeError):
    pass


class PendergrassSSOClient:
    def __init__(self, sso_url, client_id, client_secret, redirect_uri, timeout=10):
        self.sso_url = sso_url.rstrip('/')
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.timeout = timeout

    def get_login_url(self, state=None, scope='openid profile email', nonce=None):
        params = {
            'response_type': 'code',
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'scope': scope,
        }
        if state:
            params['state'] = state
        if nonce:
            params['nonce'] = nonce
        return f"{self._url('/oauth/authorize')}?{urlencode(params)}"

    def exchange_code(self, code):
        return self._post_form('/oauth/token', {
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'redirect_uri': self.redirect_uri,
        })

    def refresh_token(self, refresh_token, scope='openid profile email'):
        return self._post_form('/oauth/token', {
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'scope': scope,
        })

    def get_user_info(self, access_token):
        return self._request('GET', '/oauth/userinfo', headers={
            'Authorization': f'Bearer {access_token}',
        })

    def verify_token(self, access_token):
        return self._request('GET', '/oauth/verify', params={'access_token': access_token})

    def revoke_token(self, refresh_token):
        return self._post_form('/oauth/revoke', {
            'token': refresh_token,
            'token_type_hint': 'refresh_token',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        })

    def get_logout_url(self, redirect_uri=None):
        params = {}
        if redirect_uri:
            params['redirect_uri'] = redirect_uri
        query = f"?{urlencode(params)}" if params else ''
        return f"{self._url('/logout')}{query}"

    def _post_form(self, path, data):
        return self._request('POST', path, data=data)

    def _request(self, method, path, headers=None, params=None, data=None):
        response = requests.request(
            method,
            self._url(path),
            headers=headers,
            params=params,
            data=data,
            timeout=self.timeout,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.ok:
            return payload if payload is not None else {}

        message = None
        if isinstance(payload, dict):
            message = payload.get('error_description') or payload.get('error') or payload.get('message')
        if not message:
            message = response.text.strip() or f'SSO request failed with HTTP {response.status_code}'
        raise PendergrassSSOError(message)

    def _url(self, path):
        return urljoin(f'{self.sso_url}/', path.lstrip('/'))
