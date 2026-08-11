import os

from dotenv import load_dotenv
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.utils import safe_join

from auth import auth_bp
from routes.automation import automation_bp
from models import db
from routes.campaigns import campaigns_bp
from routes.characters import characters_bp
from routes.clarification_forks import clarification_forks_bp
from routes.dev import dev_bp
from routes.encounter_maps import encounter_maps_bp
from routes.members import members_bp
from routes.planning import planning_bp
from routes.sessions import sessions_bp
from routes.lootboxes import lootboxes_bp
from routes.llm_players import llm_players_bp
from routes.world import world_bp
from routes.shops import shops_bp
from sqldb_config import configure_sqlite_engine_options, install_sqlite_pragmas

load_dotenv()
llm_campaign_env = os.environ.get('LLM_CAMPAIGN_ENV_FILE')
if llm_campaign_env and os.path.exists(llm_campaign_env):
    load_dotenv(llm_campaign_env)



def create_app():
    # Ensure consistent absolute instance path relative to project root
    server_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(server_dir)
    instance_dir = os.path.join(project_root, 'instance')
    os.makedirs(instance_dir, exist_ok=True)

    app = Flask(__name__, static_folder=None, instance_path=instance_dir)

    frontend_origins = os.environ.get('FRONTEND_ORIGINS', '*')
    cors_origins = '*' if frontend_origins == '*' else [
        origin.strip()
        for origin in frontend_origins.split(',')
        if origin.strip()
    ]
    CORS(app, resources={r'/api/*': {'origins': cors_origins}})

    default_db_path = f"sqlite:///{os.path.join(instance_dir, 'dnd.db')}"
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', default_db_path)
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')
    app.config['JWT_EXPIRATION_HOURS'] = int(os.environ.get('JWT_EXPIRATION_HOURS', 24))
    app.config['AUTH_SESSION_COOKIE_NAME'] = os.environ.get('AUTH_SESSION_COOKIE_NAME', 'dnd_session')
    app.config['AUTH_SESSION_LIFETIME_DAYS'] = int(os.environ.get('AUTH_SESSION_LIFETIME_DAYS', 30))
    app.config['OAUTH_STATE_LIFETIME_MINUTES'] = int(os.environ.get('OAUTH_STATE_LIFETIME_MINUTES', 10))
    app.config['AUTH_COOKIE_SECURE'] = os.environ.get('AUTH_COOKIE_SECURE', 'false').lower() == 'true'
    app.config['API_KEY_LAST_USED_WRITE_INTERVAL_SECONDS'] = int(
        os.environ.get('API_KEY_LAST_USED_WRITE_INTERVAL_SECONDS', '300')
    )

    configure_sqlite_engine_options(app)
    db.init_app(app)
    install_sqlite_pragmas(db, app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(automation_bp)
    app.register_blueprint(campaigns_bp)
    app.register_blueprint(characters_bp)
    app.register_blueprint(clarification_forks_bp)
    app.register_blueprint(dev_bp)
    app.register_blueprint(encounter_maps_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(planning_bp)
    app.register_blueprint(sessions_bp)
    app.register_blueprint(lootboxes_bp)
    app.register_blueprint(llm_players_bp)
    app.register_blueprint(world_bp)
    app.register_blueprint(shops_bp)

    @app.route('/api/health')
    def health():
        return jsonify({'status': 'ok'})

    @app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
    @app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
    def serve_frontend(path):
        if path.startswith('api/'):
            return jsonify({'error': 'Not found'}), 404

        static_dir = os.path.join(app.root_path, 'static')
        requested_path = safe_join(static_dir, path) if path else None
        if requested_path and os.path.isfile(requested_path):
            return send_from_directory(static_dir, path)

        index_path = os.path.join(static_dir, 'index.html')
        if os.path.exists(index_path):
            return send_from_directory(static_dir, 'index.html')

        return jsonify({'status': 'ok', 'message': 'API server is running'}), 200

    return app


def initialize_database(app):
    with app.app_context():
        db.create_all()
        from schema_reconciliation import reconcile_schema
        reconcile_schema(app)
        reconcile_stale_awaiting_audit_runs()
        bootstrap_owner_api_key()
        print("Database initialization completed.", flush=True)


def reconcile_stale_awaiting_audit_runs():
    """Continue runs stuck in awaiting_audit whose audit cycle is already audited."""
    from services.automation_service import reconcile_stale_awaiting_audit_runs as reconcile
    reconciled = reconcile()
    if reconciled:
        print(f"Reconciled {reconciled} stale awaiting_audit run(s).", flush=True)


def bootstrap_owner_api_key():
    """Ensure that the DND_OWNER_API_KEY from environment is registered in the database on startup."""
    owner_key = os.environ.get('DND_OWNER_API_KEY')
    if not owner_key:
        return

    from models import User, UserAutomationKey
    from werkzeug.security import generate_password_hash
    from sqlalchemy.exc import IntegrityError

    prefix = owner_key[:24]
    existing_key = UserAutomationKey.query.filter_by(api_key_prefix=prefix).first()
    if existing_key:
        return

    # Check if we have an owner/admin user
    username = 'owner'
    owner_user = User.query.filter_by(username=username).first()
    if not owner_user:
        import secrets
        owner_user = User(
            username=username,
            email='owner@pendergrass-sso.local',
        )
        owner_user.set_password(secrets.token_urlsafe(32))
        db.session.add(owner_user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # If insert failed, another worker probably inserted it concurrently.
            owner_user = User.query.filter_by(username=username).first()
            if not owner_user:
                return

    # Register the automation key for this user
    automation_key = UserAutomationKey(
        user_id=owner_user.id,
        label='deploy-host',
        api_key_hash=generate_password_hash(owner_key),
        api_key_prefix=prefix,
    )
    db.session.add(automation_key)
    try:
        db.session.commit()
        print(f"Bootstrapped owner API key prefix: {prefix} for user: {username}", flush=True)
    except IntegrityError:
        db.session.rollback()
        # Key was already inserted by another worker
        pass

app = create_app()


if __name__ == '__main__':
    initialize_database(app)
    port = int(os.environ.get('PORT', 5889))
    debug = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port)
