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
        ensure_lightweight_schema()
        verify_required_schema()
        reconcile_stale_awaiting_audit_runs()
        bootstrap_owner_api_key()
        print("Database initialization completed.", flush=True)


def verify_required_schema():
    """Fail startup loudly if the database is missing model-required columns."""
    missing = []
    for table in db.metadata.sorted_tables:
        existing = {
            row[1]
            for row in db.session.execute(text(f'PRAGMA table_info({table.name})')).fetchall()
        }
        if not existing:
            missing.append(f'{table.name} (table missing)')
            continue
        for column in table.columns:
            if column.name not in existing:
                missing.append(f'{table.name}.{column.name}')
    if missing:
        raise RuntimeError(
            'Database schema is incompatible with the application models; '
            'missing required tables/columns: ' + ', '.join(sorted(missing))
        )


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
    if 'memory_anchors' not in campaign_session_columns:
        db.session.execute(text('ALTER TABLE campaign_sessions ADD COLUMN memory_anchors JSON'))

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
        if 'scorecard_template_id' not in automation_scenario_columns:
            db.session.execute(text('ALTER TABLE automation_scenarios ADD COLUMN scorecard_template_id INTEGER'))
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
        if 'scorecard_template_json' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN scorecard_template_json JSON'))
            db.session.execute(text("UPDATE automation_runs SET scorecard_template_json = '{}' WHERE scorecard_template_json IS NULL"))
        if 'last_event_sequence' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN last_event_sequence INTEGER'))
        if 'awaiting_audit_cycle_id' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN awaiting_audit_cycle_id INTEGER'))
        if 'awaiting_audit_phase' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN awaiting_audit_phase VARCHAR(40)'))
        if 'audit_resumed_at' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN audit_resumed_at DATETIME'))
        if 'last_claim_attempt_at' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN last_claim_attempt_at DATETIME'))
        if 'claim_failure_reason' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN claim_failure_reason TEXT'))
        if 'worker_api_base' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN worker_api_base VARCHAR(200)'))
        if 'reconciliation_player_message_id' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN reconciliation_player_message_id VARCHAR(120)'))
        if 'reconciliation_timeout_phase' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN reconciliation_timeout_phase VARCHAR(40)'))
        if 'reconciliation_timeout_error' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN reconciliation_timeout_error TEXT'))
        if 'reconciliation_started_at' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN reconciliation_started_at DATETIME'))
        if 'reconciliation_deadline' not in automation_run_columns:
            db.session.execute(text('ALTER TABLE automation_runs ADD COLUMN reconciliation_deadline DATETIME'))

    automation_event_columns = table_columns('automation_run_events')
    if automation_event_columns:
        if 'sequence_number' not in automation_event_columns:
            db.session.execute(text('ALTER TABLE automation_run_events ADD COLUMN sequence_number INTEGER DEFAULT 0 NOT NULL'))
        if 'attempt_number' not in automation_event_columns:
            db.session.execute(text('ALTER TABLE automation_run_events ADD COLUMN attempt_number INTEGER DEFAULT 0 NOT NULL'))
    attempts_columns = table_columns('automation_run_audit_attempts')
    if not attempts_columns:
        db.session.execute(text('''
            CREATE TABLE automation_run_audit_attempts (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                cycle_id INTEGER NOT NULL,
                auditor_job_id INTEGER,
                cycle_number INTEGER NOT NULL,
                phase VARCHAR(40) NOT NULL,
                attempt_source VARCHAR(40) NOT NULL DEFAULT 'built_in_auditor',
                auditor_slot INTEGER,
                provider VARCHAR(80),
                model VARCHAR(200),
                status VARCHAR(30) NOT NULL,
                error_class VARCHAR(120),
                error_message TEXT,
                raw_payload_json JSON,
                normalized_payload_json JSON,
                created_at DATETIME,
                FOREIGN KEY(run_id) REFERENCES automation_runs (id),
                FOREIGN KEY(cycle_id) REFERENCES automation_run_audit_cycles (id),
                FOREIGN KEY(auditor_job_id) REFERENCES automation_run_auditor_jobs (id)
            )
        '''))
        db.session.execute(text('CREATE INDEX ix_automation_run_audit_attempts_run_id ON automation_run_audit_attempts (run_id)'))
        db.session.execute(text('CREATE INDEX ix_automation_run_audit_attempts_cycle_id ON automation_run_audit_attempts (cycle_id)'))
        db.session.execute(text('CREATE INDEX ix_automation_run_audit_attempts_auditor_job_id ON automation_run_audit_attempts (auditor_job_id)'))
        db.session.execute(text('CREATE INDEX ix_automation_run_audit_attempts_created_at ON automation_run_audit_attempts (created_at)'))

    # --- campaign_worlds ---
    campaign_world_columns = table_columns('campaign_worlds')
    if 'memory_revision' not in campaign_world_columns:
        db.session.execute(text('ALTER TABLE campaign_worlds ADD COLUMN memory_revision INTEGER DEFAULT 0 NOT NULL'))

    # --- campaign_memory_logs ---
    memory_log_columns = table_columns('campaign_memory_logs')
    if memory_log_columns:
        if 'evidence_status' not in memory_log_columns:
            db.session.execute(text('ALTER TABLE campaign_memory_logs ADD COLUMN evidence_status VARCHAR(50)'))
        if 'provenance_json' not in memory_log_columns:
            db.session.execute(text('ALTER TABLE campaign_memory_logs ADD COLUMN provenance_json JSON'))

    # --- campaign_clocks ---
    clock_columns = table_columns('campaign_clocks')
    if clock_columns and 'completion_criteria' not in clock_columns:
        db.session.execute(text('ALTER TABLE campaign_clocks ADD COLUMN completion_criteria JSON'))

    # --- New tables for session memory integrity ---
    resolver_packet_columns = table_columns('campaign_resolver_packets')
    if not resolver_packet_columns:
        db.session.execute(text('''
            CREATE TABLE campaign_resolver_packets (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                session_id INTEGER,
                dm_message_id INTEGER,
                turn_id VARCHAR(100),
                packet_json JSON NOT NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'committed',
                accepted_at DATETIME NOT NULL,
                FOREIGN KEY(campaign_id) REFERENCES campaign (id),
                FOREIGN KEY(session_id) REFERENCES campaign_sessions (id),
                FOREIGN KEY(dm_message_id) REFERENCES session_messages (id)
            )
        '''))
        db.session.execute(text('CREATE INDEX ix_campaign_resolver_packets_campaign_id ON campaign_resolver_packets (campaign_id)'))
        db.session.execute(text('CREATE INDEX ix_campaign_resolver_packets_dm_message_id ON campaign_resolver_packets (dm_message_id)'))

    response_parts_columns = table_columns('campaign_dm_response_parts')
    if not response_parts_columns:
        db.session.execute(text('''
            CREATE TABLE campaign_dm_response_parts (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                session_id INTEGER,
                dm_message_id INTEGER NOT NULL,
                turn_id VARCHAR(100),
                parts_json JSON NOT NULL,
                accepted_at DATETIME NOT NULL,
                FOREIGN KEY(campaign_id) REFERENCES campaign (id),
                FOREIGN KEY(session_id) REFERENCES campaign_sessions (id),
                FOREIGN KEY(dm_message_id) REFERENCES session_messages (id)
            )
        '''))
        db.session.execute(text('CREATE INDEX ix_campaign_dm_response_parts_campaign_id ON campaign_dm_response_parts (campaign_id)'))
        db.session.execute(text('CREATE INDEX ix_campaign_dm_response_parts_dm_message_id ON campaign_dm_response_parts (dm_message_id)'))

    clarification_columns = table_columns('campaign_clarifications')
    if not clarification_columns:
        db.session.execute(text('''
            CREATE TABLE campaign_clarifications (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                clarification_id VARCHAR(200) UNIQUE NOT NULL,
                idempotency_key VARCHAR(500) UNIQUE NOT NULL,
                kind VARCHAR(60) NOT NULL,
                mention_ref VARCHAR(200) NOT NULL,
                mention_entity_id VARCHAR(200),
                question TEXT NOT NULL,
                candidate_ids JSON,
                blocking_scope JSON,
                status VARCHAR(30) NOT NULL DEFAULT 'pending',
                answer TEXT,
                resolved_canonical_id VARCHAR(200),
                resolution_action VARCHAR(40),
                resolution_patch_json JSON,
                answered_by VARCHAR(200),
                answered_at DATETIME,
                dismissed_at DATETIME,
                obsoleted_by_clarification_id VARCHAR(200),
                source_memory_run_id VARCHAR(100),
                source_turn_id VARCHAR(100),
                source_trace_id VARCHAR(100),
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(campaign_id) REFERENCES campaign (id)
            )
        '''))
        db.session.execute(text('CREATE INDEX ix_campaign_clarifications_campaign_id ON campaign_clarifications (campaign_id)'))
        db.session.execute(text('CREATE INDEX ix_campaign_clarifications_status ON campaign_clarifications (status)'))
        db.session.execute(text('CREATE INDEX ix_campaign_clarifications_idempotency ON campaign_clarifications (idempotency_key)'))

    identity_res_columns = table_columns('campaign_identity_resolutions')
    if not identity_res_columns:
        db.session.execute(text('''
            CREATE TABLE campaign_identity_resolutions (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                resolution_id VARCHAR(200) UNIQUE NOT NULL,
                mention_entity_id VARCHAR(200) NOT NULL,
                mention_name VARCHAR(400),
                resolution_action VARCHAR(40) NOT NULL,
                canonical_id VARCHAR(200) NOT NULL,
                canonical_name VARCHAR(400),
                visibility VARCHAR(30) NOT NULL DEFAULT 'dm_private',
                resolved_by VARCHAR(120),
                source_clarification_id VARCHAR(200),
                source_turn_id VARCHAR(100),
                source_trace_id VARCHAR(100),
                evidence_json JSON,
                created_at DATETIME,
                FOREIGN KEY(campaign_id) REFERENCES campaign (id)
            )
        '''))
        db.session.execute(text('CREATE INDEX ix_campaign_identity_resolutions_campaign_id ON campaign_identity_resolutions (campaign_id)'))
        db.session.execute(text('CREATE INDEX ix_campaign_identity_resolutions_mention_entity ON campaign_identity_resolutions (mention_entity_id)'))
        db.session.execute(text('CREATE INDEX ix_campaign_identity_resolutions_canonical ON campaign_identity_resolutions (canonical_id)'))

    clarification_fork_columns = table_columns('campaign_clarification_forks')
    if not clarification_fork_columns:
        db.session.execute(text('''
            CREATE TABLE campaign_clarification_forks (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                clarification_id VARCHAR(200),
                anchor_message_id INTEGER,
                base_start_message_id INTEGER,
                created_by_user_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                snapshot_json JSON NOT NULL,
                resolution_json JSON,
                status VARCHAR(30) NOT NULL DEFAULT 'active',
                generation_error TEXT,
                generation_attempt_count INTEGER NOT NULL DEFAULT 0,
                memory_revision INTEGER NOT NULL DEFAULT 0,
                context_hash VARCHAR(64),
                compacted_summary TEXT,
                compacted_through_message_id INTEGER,
                created_at DATETIME NOT NULL,
                resolved_at DATETIME,
                archived_at DATETIME,
                FOREIGN KEY(campaign_id) REFERENCES campaign (id),
                FOREIGN KEY(session_id) REFERENCES campaign_sessions (id),
                FOREIGN KEY(clarification_id) REFERENCES campaign_clarifications (clarification_id),
                FOREIGN KEY(anchor_message_id) REFERENCES session_messages (id),
                FOREIGN KEY(base_start_message_id) REFERENCES session_messages (id),
                FOREIGN KEY(created_by_user_id) REFERENCES users (id)
            )
        '''))
        db.session.execute(text('CREATE INDEX ix_campaign_clarification_forks_campaign_id ON campaign_clarification_forks (campaign_id)'))
        db.session.execute(text('CREATE INDEX ix_campaign_clarification_forks_session_id ON campaign_clarification_forks (session_id)'))
        db.session.execute(text('CREATE INDEX ix_campaign_clarification_forks_status ON campaign_clarification_forks (status)'))
    else:
        if 'base_start_message_id' not in clarification_fork_columns:
            db.session.execute(text('ALTER TABLE campaign_clarification_forks ADD COLUMN base_start_message_id INTEGER'))
        if 'generation_error' not in clarification_fork_columns:
            db.session.execute(text('ALTER TABLE campaign_clarification_forks ADD COLUMN generation_error TEXT'))
        if 'generation_attempt_count' not in clarification_fork_columns:
            db.session.execute(text('ALTER TABLE campaign_clarification_forks ADD COLUMN generation_attempt_count INTEGER NOT NULL DEFAULT 0'))
        if 'memory_revision' not in clarification_fork_columns:
            db.session.execute(text('ALTER TABLE campaign_clarification_forks ADD COLUMN memory_revision INTEGER NOT NULL DEFAULT 0'))
        if 'context_hash' not in clarification_fork_columns:
            db.session.execute(text('ALTER TABLE campaign_clarification_forks ADD COLUMN context_hash VARCHAR(64)'))
        if 'compacted_summary' not in clarification_fork_columns:
            db.session.execute(text('ALTER TABLE campaign_clarification_forks ADD COLUMN compacted_summary TEXT'))
        if 'compacted_through_message_id' not in clarification_fork_columns:
            db.session.execute(text('ALTER TABLE campaign_clarification_forks ADD COLUMN compacted_through_message_id INTEGER'))

    clarification_fork_message_columns = table_columns('campaign_clarification_fork_messages')
    if not clarification_fork_message_columns:
        db.session.execute(text('''
            CREATE TABLE campaign_clarification_fork_messages (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                fork_id INTEGER NOT NULL,
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(fork_id) REFERENCES campaign_clarification_forks (id)
            )
        '''))
        db.session.execute(text('CREATE INDEX ix_campaign_clarification_fork_messages_fork_id ON campaign_clarification_fork_messages (fork_id)'))

    db.session.commit()


app = create_app()


if __name__ == '__main__':
    initialize_database(app)
    port = int(os.environ.get('PORT', 5889))
    debug = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port)
