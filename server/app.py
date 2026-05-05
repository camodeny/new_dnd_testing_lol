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
from routes.planning import planning_bp
from routes.sessions import sessions_bp
from routes.world import world_bp

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
    app.register_blueprint(planning_bp)
    app.register_blueprint(sessions_bp)
    app.register_blueprint(world_bp)

    @app.route('/api/health')
    def health():
        return jsonify({'status': 'ok'})

    with app.app_context():
        db.create_all()
        ensure_lightweight_schema()

    return app


def ensure_lightweight_schema():
    """Apply additive SQLite schema updates for dev databases without Alembic."""
    campaign_member_columns = {
        row[1]
        for row in db.session.execute(text('PRAGMA table_info(campaign_members)')).fetchall()
    }
    if 'selected_character_id' not in campaign_member_columns:
        db.session.execute(text('ALTER TABLE campaign_members ADD COLUMN selected_character_id INTEGER'))
    if 'character_ready_at' not in campaign_member_columns:
        db.session.execute(text('ALTER TABLE campaign_members ADD COLUMN character_ready_at DATETIME'))
    db.session.commit()


app = create_app()


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5889)
