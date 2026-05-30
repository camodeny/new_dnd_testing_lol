import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import jwt
from flask import Blueprint, current_app, g, jsonify, redirect, request
from werkzeug.security import check_password_hash

from models import AuthSession, LLMPlayer, User, db
from pendergrass_sso import PendergrassSSOClient, PendergrassSSOError

auth_bp = Blueprint('auth', __name__)

SESSION_COOKIE_NAME = 'dnd_session'
SESSION_LIFETIME_DAYS = 30
OAUTH_STATE_LIFETIME_MINUTES = 10
SSO_PROVIDER_NAME = 'pendergrass_sso'


def generate_token(user_id):
    payload = {
        'user_id': user_id,
        'exp': datetime.now(timezone.utc) + timedelta(hours=current_app.config['JWT_EXPIRATION_HOURS']),
        'iat': datetime.now(timezone.utc)
    }
    return jwt.encode(payload, current_app.config['SECRET_KEY'], algorithm='HS256')


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        current_user, error_response = authenticate_request()
        if error_response is not None:
            return error_response

        return f(current_user, *args, **kwargs)
    return decorated


def _extract_bearer_token():
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header.split(' ', 1)[1].strip()
    return None


def _hash_session_token(session_token):
    return hashlib.sha256(session_token.encode('utf-8')).hexdigest()


def _get_session_cookie_name():
    return current_app.config.get('AUTH_SESSION_COOKIE_NAME', SESSION_COOKIE_NAME)


def _cookie_secure():
    return current_app.config.get('AUTH_COOKIE_SECURE', False)


def _set_session_cookie(response, session_token):
    response.set_cookie(
        _get_session_cookie_name(),
        session_token,
        httponly=True,
        samesite='Lax',
        secure=_cookie_secure(),
        max_age=int(timedelta(days=current_app.config.get('AUTH_SESSION_LIFETIME_DAYS', SESSION_LIFETIME_DAYS)).total_seconds()),
        path='/',
    )
    return response


def _clear_session_cookie(response):
    response.delete_cookie(_get_session_cookie_name(), path='/')
    return response


def _get_auth_session_from_token(session_token):
    if not session_token:
        return None
    return AuthSession.query.filter_by(session_token_hash=_hash_session_token(session_token)).first()


def _get_auth_session_from_request():
    return _get_auth_session_from_token(request.cookies.get(_get_session_cookie_name()))


def _create_auth_session(user=None, provider=None):
    raw_session_token = secrets.token_urlsafe(48)
    auth_session = AuthSession(
        session_token_hash=_hash_session_token(raw_session_token),
        user_id=user.id if user else None,
        provider=provider,
        last_seen_at=datetime.utcnow(),
    )
    db.session.add(auth_session)
    db.session.commit()
    return auth_session, raw_session_token


def _ensure_auth_session():
    session_token = request.cookies.get(_get_session_cookie_name())
    auth_session = _get_auth_session_from_token(session_token)
    if auth_session is not None:
        return auth_session, session_token, False
    auth_session, session_token = _create_auth_session()
    return auth_session, session_token, True


def _delete_auth_session(auth_session):
    if not auth_session:
        return
    db.session.delete(auth_session)
    db.session.commit()


def _finalize_auth_session(auth_session, user, *, provider=None, provider_subject=None, tokens=None, provider_user=None):
    auth_session.user_id = user.id
    auth_session.provider = provider
    auth_session.provider_subject = provider_subject
    auth_session.last_seen_at = datetime.utcnow()
    auth_session.pending_oauth_state = None
    auth_session.pending_oauth_state_expires_at = None
    auth_session.post_login_redirect = None

    if tokens:
        expires_in = tokens.get('expires_in')
        auth_session.access_token = tokens.get('access_token')
        auth_session.refresh_token = tokens.get('refresh_token')
        auth_session.id_token = tokens.get('id_token')
        auth_session.scope = tokens.get('scope')
        auth_session.access_token_expires_at = (
            datetime.utcnow() + timedelta(seconds=int(expires_in))
            if expires_in is not None else None
        )

    if provider_user is not None:
        auth_session.provider_user_json = json.dumps(provider_user)

    db.session.commit()


def _authenticate_jwt(token):
    try:
        data = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
        current_user = db.session.get(User, data['user_id'])
        if not current_user:
            return None, jsonify({'error': 'User not found'}), 401
        g.auth_mode = 'jwt'
        g.llm_player = None
        g.auth_session = None
        return current_user, None, None
    except jwt.ExpiredSignatureError:
        return None, jsonify({'error': 'Token has expired'}), 401
    except jwt.InvalidTokenError:
        return None, jsonify({'error': 'Token is invalid'}), 401


def _authenticate_api_key(api_key):
    if not api_key:
        return None, jsonify({'error': 'API key is missing'}), 401

    prefix = api_key[:24]
    candidates = LLMPlayer.query.filter_by(api_key_prefix=prefix).all()
    for llm_player in candidates:
        if check_password_hash(llm_player.api_key_hash, api_key):
            current_user = llm_player.user
            if not current_user:
                return None, jsonify({'error': 'User not found'}), 401
            llm_player.last_used_at = datetime.utcnow()
            db.session.commit()
            g.auth_mode = 'api_key'
            g.llm_player = llm_player
            g.auth_session = None
            return current_user, None, None

    return None, jsonify({'error': 'API key is invalid'}), 401


def _authenticate_session_cookie(session_token):
    auth_session = _get_auth_session_from_token(session_token)
    if not auth_session or not auth_session.user_id:
        return None, jsonify({'error': 'Token is missing'}), 401

    current_user = db.session.get(User, auth_session.user_id)
    if not current_user:
        return None, jsonify({'error': 'User not found'}), 401

    auth_session.last_seen_at = datetime.utcnow()
    db.session.commit()
    g.auth_mode = 'session'
    g.llm_player = None
    g.auth_session = auth_session
    return current_user, None, None


def authenticate_request(token=None, api_key=None):
    provided_api_key = api_key or request.headers.get('X-API-Key')
    bearer_token = token or _extract_bearer_token()

    if provided_api_key:
        current_user, response, status = _authenticate_api_key(provided_api_key)
        if response is not None:
            return None, (response, status)
        return current_user, None

    if bearer_token:
        if bearer_token.startswith('dndllm_'):
            current_user, response, status = _authenticate_api_key(bearer_token)
            if response is not None:
                return None, (response, status)
            return current_user, None

        current_user, response, status = _authenticate_jwt(bearer_token)
        if response is not None:
            return None, (response, status)
        return current_user, None

    session_token = request.cookies.get(_get_session_cookie_name())
    if session_token:
        current_user, response, status = _authenticate_session_cookie(session_token)
        if response is not None:
            return None, (response, status)
        return current_user, None

    return None, (jsonify({'error': 'Token is missing'}), 401)


def pendergrass_sso_enabled():
    return all([
        os.environ.get('SSO_URL'),
        os.environ.get('CLIENT_ID'),
        os.environ.get('CLIENT_SECRET'),
        os.environ.get('REDIRECT_URI'),
    ])


def get_pendergrass_sso_client():
    if not pendergrass_sso_enabled():
        raise PendergrassSSOError('Pendergrass SSO is not configured.')
    return PendergrassSSOClient(
        sso_url=os.environ['SSO_URL'],
        client_id=os.environ['CLIENT_ID'],
        client_secret=os.environ['CLIENT_SECRET'],
        redirect_uri=os.environ['REDIRECT_URI'],
    )


def _is_safe_redirect_target(target):
    if not target or not target.startswith('/'):
        return False
    if target.startswith('//'):
        return False
    parts = urlsplit(target)
    return not parts.scheme and not parts.netloc


def _build_redirect_target(base_path, params=None):
    params = {k: v for k, v in (params or {}).items() if v}
    parts = urlsplit(base_path)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _resolve_post_login_redirect(default='/'):
    requested_target = request.args.get('next') or default
    return requested_target if _is_safe_redirect_target(requested_target) else default


def _slugify_username(value):
    base = re.sub(r'[^a-z0-9_]+', '_', (value or '').strip().lower()).strip('_')
    return base[:60] or 'adventurer'


def _unique_username(base_username):
    candidate = _slugify_username(base_username)
    suffix = 1
    while User.query.filter_by(username=candidate).first():
        suffix += 1
        candidate = f'{_slugify_username(base_username)[:55]}_{suffix}'
    return candidate


def _provision_sso_user(userinfo):
    subject = (userinfo.get('sub') or '').strip()
    if not subject:
        raise PendergrassSSOError('SSO user profile did not include a subject.')

    existing = User.query.filter_by(sso_subject=subject).first()
    if existing:
        return existing

    email = (userinfo.get('email') or '').strip().lower()
    if email:
        user = User.query.filter_by(email=email).first()
        if user:
            user.sso_subject = subject
            db.session.commit()
            return user

    username_hint = (
        userinfo.get('username')
        or userinfo.get('name')
        or (email.split('@', 1)[0] if email else '')
        or subject[:24]
    )
    username = _unique_username(username_hint)
    if not email:
        email = f'{username}@users.pendergrass-sso.local'

    user = User(
        username=username,
        email=email,
        sso_subject=subject,
    )
    user.set_password(secrets.token_urlsafe(32))
    db.session.add(user)
    db.session.commit()
    return user


@auth_bp.route('/api/auth/config', methods=['GET'])
def auth_config():
    return jsonify({'sso_enabled': pendergrass_sso_enabled()}), 200


@auth_bp.route('/api/auth/login', methods=['GET'])
def begin_sso_login():
    if not pendergrass_sso_enabled():
        return jsonify({'error': 'Pendergrass SSO is not configured'}), 503

    auth_session, session_token, _created = _ensure_auth_session()
    state = secrets.token_urlsafe(32)
    auth_session.pending_oauth_state = state
    auth_session.pending_oauth_state_expires_at = datetime.utcnow() + timedelta(
        minutes=current_app.config.get('OAUTH_STATE_LIFETIME_MINUTES', OAUTH_STATE_LIFETIME_MINUTES)
    )
    auth_session.post_login_redirect = _resolve_post_login_redirect('/')
    db.session.commit()

    response = redirect(get_pendergrass_sso_client().get_login_url(state))
    _set_session_cookie(response, session_token)
    return response


@auth_bp.route('/api/auth/callback', methods=['GET'])
def complete_sso_login():
    default_redirect = '/'
    auth_session = _get_auth_session_from_request()
    fallback_redirect = default_redirect
    if auth_session and auth_session.post_login_redirect:
        fallback_redirect = auth_session.post_login_redirect

    provider_error = request.args.get('error')
    if provider_error:
        return redirect(_build_redirect_target(fallback_redirect, {
            'auth_error': request.args.get('error_description') or provider_error,
        }))

    if not pendergrass_sso_enabled():
        return redirect(_build_redirect_target(fallback_redirect, {'auth_error': 'SSO is not configured.'}))

    if not auth_session:
        return redirect(_build_redirect_target(fallback_redirect, {'auth_error': 'Missing login session.'}))

    code = (request.args.get('code') or '').strip()
    state = (request.args.get('state') or '').strip()
    if not code:
        return redirect(_build_redirect_target(fallback_redirect, {'auth_error': 'Missing authorization code.'}))

    if (
        not auth_session.pending_oauth_state
        or auth_session.pending_oauth_state != state
        or (
            auth_session.pending_oauth_state_expires_at
            and auth_session.pending_oauth_state_expires_at < datetime.utcnow()
        )
    ):
        return redirect(_build_redirect_target(fallback_redirect, {'auth_error': 'Invalid or expired login state.'}))

    try:
        sso = get_pendergrass_sso_client()
        tokens = sso.exchange_code(code)
        userinfo = sso.get_user_info(tokens['access_token'])
        user = _provision_sso_user(userinfo)
        _finalize_auth_session(
            auth_session,
            user,
            provider=SSO_PROVIDER_NAME,
            provider_subject=userinfo.get('sub'),
            tokens=tokens,
            provider_user=userinfo,
        )
    except (KeyError, PendergrassSSOError) as exc:
        return redirect(_build_redirect_target(fallback_redirect, {'auth_error': str(exc)}))

    response = redirect(fallback_redirect)
    session_token = request.cookies.get(_get_session_cookie_name())
    if session_token:
        _set_session_cookie(response, session_token)
    return response


@auth_bp.route('/api/register', methods=['POST'])
def register():
    data = request.get_json()

    if not data or not data.get('username') or not data.get('password') or not data.get('email'):
        return jsonify({'error': 'Missing required fields'}), 400

    if User.query.filter_by(username=data['username']).first():
        return jsonify({'error': 'Username already exists'}), 400

    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'Email already exists'}), 400

    user = User(username=data['username'], email=data['email'])
    user.set_password(data['password'])

    db.session.add(user)
    db.session.commit()

    token = generate_token(user.id)
    _auth_session, session_token = _create_auth_session(user=user)
    response = jsonify({'message': 'User created successfully', 'token': token, 'user': user.to_dict()})
    _set_session_cookie(response, session_token)
    return response, 201


@auth_bp.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()

    if not data or not data.get('username') or not data.get('password'):
        return jsonify({'error': 'Missing username or password'}), 400

    user = User.query.filter_by(username=data['username']).first()

    if not user or not user.check_password(data['password']):
        return jsonify({'error': 'Invalid username or password'}), 401

    token = generate_token(user.id)
    _auth_session, session_token = _create_auth_session(user=user)
    response = jsonify({'message': 'Login successful', 'token': token, 'user': user.to_dict()})
    _set_session_cookie(response, session_token)
    return response, 200


@auth_bp.route('/api/logout', methods=['POST'])
def logout():
    auth_session = _get_auth_session_from_request()
    if auth_session and auth_session.provider == SSO_PROVIDER_NAME and auth_session.refresh_token:
        try:
            get_pendergrass_sso_client().revoke_token(auth_session.refresh_token)
        except PendergrassSSOError:
            pass

    if auth_session:
        _delete_auth_session(auth_session)

    response = jsonify({'message': 'Logged out successfully'})
    _clear_session_cookie(response)
    return response, 200


@auth_bp.route('/api/me', methods=['GET'])
@token_required
def get_me(current_user):
    return jsonify({'user': current_user.to_dict()}), 200
