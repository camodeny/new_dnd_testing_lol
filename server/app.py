import os

from dotenv import load_dotenv
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import text
from werkzeug.utils import safe_join

from auth import auth_bp
from routes.automation import automation_bp
from models import db
from routes.campaigns import campaigns_bp
from routes.characters import characters_bp
from routes.dev import dev_bp
from routes.encounter_maps import encounter_maps_bp
from routes.members import members_bp
from routes.planning import planning_bp
from routes.sessions import sessions_bp
from routes.lootboxes import lootboxes_bp
from routes.llm_players import llm_players_bp
from routes.world import world_bp
from routes.shops import shops_bp

load_dotenv()


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

    db.init_app(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(automation_bp)
    app.register_blueprint(campaigns_bp)
    app.register_blueprint(characters_bp)
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

    with app.app_context():
        db.create_all()
        ensure_lightweight_schema()

    return app


def ensure_lightweight_schema():
    """Apply additive SQLite schema updates for dev databases without Alembic."""
    def table_columns(table_name):
        return {
            row[1]
            for row in db.session.execute(text(f'PRAGMA table_info({table_name})')).fetchall()
        }

    campaign_member_columns = table_columns('campaign_members')
    if 'selected_character_id' not in campaign_member_columns:
        db.session.execute(text('ALTER TABLE campaign_members ADD COLUMN selected_character_id INTEGER'))
    if 'character_ready_at' not in campaign_member_columns:
        db.session.execute(text('ALTER TABLE campaign_members ADD COLUMN character_ready_at DATETIME'))

    session_message_columns = table_columns('session_messages')
    if 'user_id' not in session_message_columns:
        db.session.execute(text('ALTER TABLE session_messages ADD COLUMN user_id INTEGER'))

    campaign_session_columns = table_columns('campaign_sessions')
    if 'running_summary' not in campaign_session_columns:
        db.session.execute(text('ALTER TABLE campaign_sessions ADD COLUMN running_summary TEXT'))

    audit_event_columns = table_columns('campaign_audit_events')
    if 'trace_id' not in audit_event_columns:
        db.session.execute(text('ALTER TABLE campaign_audit_events ADD COLUMN trace_id VARCHAR(160)'))
    if 'parent_trace_id' not in audit_event_columns:
        db.session.execute(text('ALTER TABLE campaign_audit_events ADD COLUMN parent_trace_id VARCHAR(160)'))
    if 'trace_label' not in audit_event_columns:
        db.session.execute(text('ALTER TABLE campaign_audit_events ADD COLUMN trace_label VARCHAR(200)'))
    if 'audit_role' not in audit_event_columns:
        db.session.execute(text('ALTER TABLE campaign_audit_events ADD COLUMN audit_role VARCHAR(20)'))

    embedding_columns = table_columns('campaign_memory_embeddings')
    if embedding_columns:
        if 'visibility' not in embedding_columns:
            db.session.execute(text('ALTER TABLE campaign_memory_embeddings ADD COLUMN visibility VARCHAR(30) DEFAULT "dm_private"'))
        if 'embedding_dimensions' not in embedding_columns:
            db.session.execute(text('ALTER TABLE campaign_memory_embeddings ADD COLUMN embedding_dimensions INTEGER DEFAULT 0'))

    encounter_map_columns = table_columns('encounter_maps')
    if encounter_map_columns:
        if 'labeled_image_filename' not in encounter_map_columns:
            db.session.execute(text('ALTER TABLE encounter_maps ADD COLUMN labeled_image_filename VARCHAR(260)'))
        if 'grid_json' not in encounter_map_columns:
            db.session.execute(text('ALTER TABLE encounter_maps ADD COLUMN grid_json TEXT'))
        if 'vtt_setup_json' not in encounter_map_columns:
            db.session.execute(text('ALTER TABLE encounter_maps ADD COLUMN vtt_setup_json TEXT'))
        if 'encounter_state_json' not in encounter_map_columns:
            db.session.execute(text('ALTER TABLE encounter_maps ADD COLUMN encounter_state_json TEXT'))
        if 'setup_status' not in encounter_map_columns:
            db.session.execute(text('ALTER TABLE encounter_maps ADD COLUMN setup_status VARCHAR(20) DEFAULT "pending"'))
        if 'setup_error' not in encounter_map_columns:
            db.session.execute(text('ALTER TABLE encounter_maps ADD COLUMN setup_error VARCHAR(500)'))
        if 'is_archived' not in encounter_map_columns:
            db.session.execute(text('ALTER TABLE encounter_maps ADD COLUMN is_archived BOOLEAN DEFAULT 0 NOT NULL'))

    campaign_shop_columns = table_columns('campaign_shops')
    if campaign_shop_columns:
        if 'location_id' not in campaign_shop_columns:
            db.session.execute(text('ALTER TABLE campaign_shops ADD COLUMN location_id VARCHAR(160)'))
        if 'location_name' not in campaign_shop_columns:
            db.session.execute(text('ALTER TABLE campaign_shops ADD COLUMN location_name VARCHAR(200)'))
        if 'is_open' not in campaign_shop_columns:
            db.session.execute(text('ALTER TABLE campaign_shops ADD COLUMN is_open BOOLEAN DEFAULT 1 NOT NULL'))

    user_columns = table_columns('users')
    if 'sso_subject' not in user_columns:
        db.session.execute(text('ALTER TABLE users ADD COLUMN sso_subject VARCHAR(160)'))

    campaign_columns = table_columns('campaign')
    if 'is_automation_clone' not in campaign_columns:
        db.session.execute(text('ALTER TABLE campaign ADD COLUMN is_automation_clone BOOLEAN DEFAULT 0 NOT NULL'))
    if 'automation_source_campaign_id' not in campaign_columns:
        db.session.execute(text('ALTER TABLE campaign ADD COLUMN automation_source_campaign_id INTEGER'))
    if 'automation_source_snapshot_id' not in campaign_columns:
        db.session.execute(text('ALTER TABLE campaign ADD COLUMN automation_source_snapshot_id INTEGER'))
    if 'automation_source_run_id' not in campaign_columns:
        db.session.execute(text('ALTER TABLE campaign ADD COLUMN automation_source_run_id INTEGER'))

    automation_scenario_columns = table_columns('automation_scenarios')
    if automation_scenario_columns:
        if 'baseline_run_id' not in automation_scenario_columns:
            db.session.execute(text('ALTER TABLE automation_scenarios ADD COLUMN baseline_run_id INTEGER'))
        if 'retention_policy_json' not in automation_scenario_columns:
            db.session.execute(text('ALTER TABLE automation_scenarios ADD COLUMN retention_policy_json JSON'))
            db.session.execute(text("UPDATE automation_scenarios SET retention_policy_json = '{}' WHERE retention_policy_json IS NULL"))

    automation_run_columns = table_columns('automation_runs')
    if automation_run_columns:
        if 'lease_token' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN lease_token VARCHAR(64)'))
        if 'heartbeat_at' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN heartbeat_at DATETIME'))
        if 'lease_expires_at' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN lease_expires_at DATETIME'))
        if 'attempt_count' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN attempt_count INTEGER DEFAULT 0 NOT NULL'))
        if 'reclaim_count' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN reclaim_count INTEGER DEFAULT 0 NOT NULL'))
        if 'matrix_group_id' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN matrix_group_id VARCHAR(120)'))
        if 'matrix_label' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN matrix_label VARCHAR(200)'))
        if 'baseline_comparison_json' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN baseline_comparison_json JSON'))
            db.session.execute(text("UPDATE automation_runs SET baseline_comparison_json = '{}' WHERE baseline_comparison_json IS NULL"))
        if 'clone_retention_status' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN clone_retention_status VARCHAR(30) DEFAULT "active" NOT NULL'))
        if 'clone_retention_expires_at' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN clone_retention_expires_at DATETIME'))
        if 'last_event_sequence' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN last_event_sequence INTEGER'))

    automation_event_columns = table_columns('automation_run_events')
    if automation_event_columns:
        if 'sequence_number' not in automation_event_columns:
            db.session.execute(text('ALTER TABLE automation_run_events ADD COLUMN sequence_number INTEGER DEFAULT 0 NOT NULL'))
        if 'attempt_number' not in automation_event_columns:
            db.session.execute(text('ALTER TABLE automation_run_events ADD COLUMN attempt_number INTEGER DEFAULT 0 NOT NULL'))
        if 'dedupe_key' not in automation_event_columns:
            db.session.execute(text('ALTER TABLE automation_run_events ADD COLUMN dedupe_key VARCHAR(160)'))
    db.session.commit()


app = create_app()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5889))
    debug = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port)
