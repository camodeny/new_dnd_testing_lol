from flask import Blueprint, jsonify, request, current_app, g
from datetime import datetime, timedelta, timezone
from functools import wraps
import jwt
from werkzeug.security import check_password_hash

from models import db, LLMPlayer, User

auth_bp = Blueprint('auth', __name__)


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


def _authenticate_jwt(token):
    try:
        data = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
        current_user = db.session.get(User, data['user_id'])
        if not current_user:
            return None, jsonify({'error': 'User not found'}), 401
        g.auth_mode = 'jwt'
        g.llm_player = None
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
            return current_user, None, None

    return None, jsonify({'error': 'API key is invalid'}), 401


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

    return None, (jsonify({'error': 'Token is missing'}), 401)


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
    return jsonify({'message': 'User created successfully', 'token': token, 'user': user.to_dict()}), 201


@auth_bp.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()

    if not data or not data.get('username') or not data.get('password'):
        return jsonify({'error': 'Missing username or password'}), 400

    user = User.query.filter_by(username=data['username']).first()

    if not user or not user.check_password(data['password']):
        return jsonify({'error': 'Invalid username or password'}), 401

    token = generate_token(user.id)
    return jsonify({'message': 'Login successful', 'token': token, 'user': user.to_dict()}), 200


@auth_bp.route('/api/logout', methods=['POST'])
def logout():
    return jsonify({'message': 'Logged out successfully'}), 200


@auth_bp.route('/api/me', methods=['GET'])
@token_required
def get_me(current_user):
    return jsonify({'user': current_user.to_dict()}), 200
