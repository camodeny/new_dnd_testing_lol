import os

from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_cors import CORS
from sqlalchemy import text

from auth import auth_bp
from models import db
from routes.campaigns import campaigns_bp
from routes.characters import characters_bp
from routes.dev import dev_bp
from routes.members import members_bp
from routes.sessions import sessions_bp

load_dotenv()


def create_app():
    app = Flask(__name__)
    CORS(app, resources={r'/api/*': {'origins': '*'}})

    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///dnd.db'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')
    app.config['JWT_EXPIRATION_HOURS'] = int(os.environ.get('JWT_EXPIRATION_HOURS', 24))

    db.init_app(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(campaigns_bp)
    app.register_blueprint(characters_bp)
    app.register_blueprint(dev_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(sessions_bp)

    @app.route('/api/health')
    def health():
        return jsonify({'status': 'ok'})

    with app.app_context():
        db.create_all()
        try:
            ensure_legacy_columns()
        except Exception:
            pass

    return app


def ensure_legacy_columns():
    with db.engine.connect() as conn:
        result = conn.execute(text('PRAGMA table_info(campaign)'))
        columns = {row[1] for row in result.fetchall()}
        if 'status' not in columns:
            conn.execute(text("ALTER TABLE campaign ADD COLUMN status VARCHAR DEFAULT 'active'"))
        if 'last_played_at' not in columns:
            conn.execute(text('ALTER TABLE campaign ADD COLUMN last_played_at DATETIME'))
        if 'settings' not in columns:
            conn.execute(text('ALTER TABLE campaign ADD COLUMN settings TEXT'))
        if 'invite_code' not in columns:
            conn.execute(text('ALTER TABLE campaign ADD COLUMN invite_code VARCHAR(20)'))
        conn.commit()


app = create_app()


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5889)
