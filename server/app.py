from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from functools import wraps
import jwt
import os
from models import db, User

load_dotenv()

app = Flask(__name__)
CORS(app)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///dnd.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')
app.config['JWT_EXPIRATION_HOURS'] = int(os.environ.get('JWT_EXPIRATION_HOURS', 24))

db.init_app(app)

with app.app_context():
    db.create_all()


def generate_token(user_id):
    payload = {
        'user_id': user_id,
        'exp': datetime.now(timezone.utc) + timedelta(hours=app.config['JWT_EXPIRATION_HOURS']),
        'iat': datetime.now(timezone.utc)
    }
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            if auth_header.startswith('Bearer '):
                token = auth_header.split(' ')[1]

        if not token:
            return jsonify({'error': 'Token is missing'}), 401

        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            current_user = User.query.get(data['user_id'])
            if not current_user:
                return jsonify({'error': 'User not found'}), 401
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token has expired'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'error': 'Token is invalid'}), 401

        return f(current_user, *args, **kwargs)
    return decorated


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/api/register', methods=['POST'])
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

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()

    if not data or not data.get('username') or not data.get('password'):
        return jsonify({'error': 'Missing username or password'}), 400

    user = User.query.filter_by(username=data['username']).first()

    if not user or not user.check_password(data['password']):
        return jsonify({'error': 'Invalid username or password'}), 401

    token = generate_token(user.id)
    return jsonify({'message': 'Login successful', 'token': token, 'user': user.to_dict()}), 200

@app.route('/api/logout', methods=['POST'])
def logout():
    # JWT is stateless; client discards the token
    return jsonify({'message': 'Logged out successfully'}), 200

@app.route('/api/me', methods=['GET'])
@token_required
def get_me(current_user):
    return jsonify({'user': current_user.to_dict()}), 200

@app.route('/api/campaigns', methods=['GET'])
@token_required
def get_campaigns(current_user):
    campaigns = current_user.campaigns
    return jsonify({'campaigns': [c.to_dict() for c in campaigns]}),

if __name__ == '__main__':
    app.run(debug=True, port=5889)