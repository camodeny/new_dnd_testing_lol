from datetime import datetime
import threading

from flask import Blueprint, current_app, jsonify, request, stream_with_context

from auth import authenticate_request, token_required
from models import (
    AutomationRun,
    AutomationRunAuditCycle,
    AutomationRunAuditAttempt,
    AutomationRunAuditorJob,
    AutomationRunAuditResult,
    AutomationRunEvent,
    AutomationRunProviderCall,
    AutomationScenario,
    AutomationScorecardTemplate,
    AutomationSnapshot,
    AutomationWorker,
    Campaign,
    Character,
    CharacterCondition,
    CharacterEquipment,
    SessionMessage,
    SheetProposal,
    User,
    db,
)
from services.automation_auditor import (
    AUDITOR_TOOL_DEFINITIONS,
    cancel_auditor_jobs_for_current_cycle,
    ensure_auditor_jobs_for_cycle,
    execute_auditor_tool,
    get_current_audit_bundle_data,
    list_auditor_jobs,
    merge_auditor_config_into_runner_config,
    normalize_auditor_config,
    run_builtin_auditors_for_current_cycle,
)
from services.audit_service import log_audit_event
from services.automation_service import (
    AUTOMATION_ACTIVE_STATUSES,
    CloneRetrievalPreflightError,
    append_run_event,
    append_workspace_event,
    baseline_run_for_scenario,
    claim_run_for_worker,
    cleanup_hidden_clone_campaigns,
    compare_runs_payload,
    continue_audit_run,
    create_audit_cycle,
    create_matrix_runs,
    create_snapshot_for_scenario,
    ensure_worker_lease,
    heartbeat_run,
    latest_session_for_run,
    merged_runner_config_for_scenario,
    persist_provider_call,
    record_worker_activity,
    provider_call_for_replay,
    refresh_run_scorecard,
    run_debug_summary,
    run_watch_payload,
    runner_config_from_request,
    scenario_roster_from_campaign,
    scorecard_template_snapshot,
    submit_audit_cycle_feedback,
    validate_scorecard_template_payload,
    visible_campaigns_for_user,
    workspace_trends_for_user,
)
from services.automation_stream import (
    iter_run_events,
    iter_workspace_events,
    run_stream_cursor,
    sse_message,
    workspace_stream_cursor,
)
from services.campaign_service import ensure_member, get_or_404
from services.character_service import character_full_dict
from services.dm_tools import SHEET_SCALAR_FIELDS
from services.stream_manager import stream_manager


automation_bp = Blueprint('automation', __name__)


def _scenario_visible_to_user(current_user, scenario):
    return scenario is not None


def _run_visible_to_user(current_user, run):
    return run is not None


def _scenario_owned_by_user(current_user, scenario):
    return _scenario_visible_to_user(current_user, scenario)


def _run_owned_by_user(current_user, run):
    return _run_visible_to_user(current_user, run)


def _scorecard_visible_to_user(current_user, scorecard):
    return scorecard is not None


def _scorecard_owned_by_user(current_user, scorecard):
    return _scorecard_visible_to_user(current_user, scorecard)


def _scenario_viewer_permissions(current_user, scenario):
    can_manage = _scenario_owned_by_user(current_user, scenario)
    return {
        'manage_scenario': can_manage,
        'create_snapshot': can_manage,
        'queue_run': can_manage,
        'update_scenario': can_manage,
        'cleanup_scenario': can_manage,
        'set_baseline': can_manage,
    }


def _run_viewer_permissions(current_user, run):
    can_manage = _run_owned_by_user(current_user, run)
    return {
        'manage_run': can_manage,
        'stop_run': can_manage,
        'submit_audit': can_manage,
        'continue_run': can_manage,
        'manage_auditors': can_manage,
    }


def _run_auditors_in_background(app, run_id):
    with app.app_context():
        try:
            run_builtin_auditors_for_current_cycle(run_id)
        except Exception:
            current_app.logger.exception('Built-in automation auditor failed for run %s', run_id)


def _workspace_payload(current_user):
    from datetime import timedelta
    scenarios = AutomationScenario.query.order_by(AutomationScenario.updated_at.desc(), AutomationScenario.id.desc()).all()
    scorecards = AutomationScorecardTemplate.query.order_by(AutomationScorecardTemplate.updated_at.desc(), AutomationScorecardTemplate.id.desc()).all()
    active_runs = AutomationRun.query.filter(
        AutomationRun.status.in_(tuple(AUTOMATION_ACTIVE_STATUSES)),
    ).order_by(AutomationRun.created_at.desc(), AutomationRun.id.desc()).all()
    recent_failures = AutomationRun.query.filter(
        AutomationRun.status.in_(('failed', 'stopped')),
    ).order_by(AutomationRun.updated_at.desc(), AutomationRun.id.desc()).limit(8).all()

    active_workers_query = AutomationWorker.query.filter(
        (AutomationWorker.last_poll_at >= datetime.utcnow() - timedelta(minutes=5)) |
        (AutomationWorker.last_heartbeat_at >= datetime.utcnow() - timedelta(minutes=5))
    ).all()
    queue_length = AutomationRun.query.filter_by(status='queued').count()

    return {
        'scorecards': [scorecard.to_dict() for scorecard in scorecards],
        'scenarios': [scenario.to_dict() for scenario in scenarios],
        'active_runs': [run.to_dict() for run in active_runs],
        'recent_failures': [run.to_dict() for run in recent_failures],
        'source_campaigns': [campaign.to_dict() for campaign in visible_campaigns_for_user(current_user)],
        'scenario_trends': workspace_trends_for_user(),
        'active_workers': [worker.to_dict() for worker in active_workers_query],
        'queue_length': queue_length,
    }


def _auth_stream_user():
    token_str = request.args.get('token')
    api_key = request.args.get('api_key')
    return authenticate_request(token=token_str, api_key=api_key)


def _last_event_id():
    return request.headers.get('Last-Event-ID') or request.args.get('last_event_id')


def _stream_response(generator):
    response = current_app.response_class(stream_with_context(generator), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


def _resolve_acting_user(run, data):
    if not run.scenario:
        return None, None

    roster = run.scenario.roster_json or []
    requested_user_id = data.get('user_id')
    requested_llm_player_id = data.get('llm_player_id')
    acting_entry = None
    for entry in roster:
        if requested_user_id is not None and entry.get('user_id') == requested_user_id:
            acting_entry = entry
            break
        if requested_llm_player_id is not None and entry.get('llm_player_id') == requested_llm_player_id:
            acting_entry = entry
            break
    if acting_entry is None:
        return None, None
    return db.session.get(User, acting_entry.get('user_id')), acting_entry


def _pending_proposal_for_entry(session_id, acting_entry, proposal_id):
    proposals = SheetProposal.query.filter_by(session_id=session_id, status='pending').all()
    for proposal in proposals:
        if proposal.id != proposal_id:
            continue
        if acting_entry.get('character_id') == proposal.character_id:
            return proposal
    return None


def _coerce_sheet_scalar_value(field, value):
    config = SHEET_SCALAR_FIELDS.get(field)
    if not config:
        raise ValueError(f'Unsupported character field: {field}')
    if config['type'] == 'bool':
        return bool(value)
    coerced = int(value)
    if 'min' in config:
        coerced = max(config['min'], coerced)
    if 'max' in config:
        coerced = min(config['max'], coerced)
    return coerced


def _automation_run_character(run, character_id):
    if not run.derived_campaign_id:
        raise ValueError('Run has no derived campaign')
    character = db.session.get(Character, character_id)
    if character is None or character.campaign_id != run.derived_campaign_id:
        raise ValueError('Character not found for this automation run')
    return character


def _apply_proposal_direct(proposal):
    character = db.session.get(Character, proposal.character_id)
    from models import CharacterCondition, CharacterEquipment

    for change in proposal.changes or []:
        field = change['field']
        after = change['after']
        if ':' in field:
            prefix, item_name = field.split(':', 1)
            prefix = prefix.strip().lower()
            item_name = item_name.strip()
            if prefix == 'condition':
                existing = CharacterCondition.query.filter_by(character_id=character.id, condition_name=item_name).first()
                if isinstance(after, dict) and after.get('count', 0) > 0:
                    if not existing:
                        db.session.add(CharacterCondition(character=character, condition_name=item_name))
                elif existing:
                    db.session.delete(existing)
            elif prefix == 'equipment':
                if isinstance(after, dict) and after.get('count', 0) > 0:
                    existing = CharacterEquipment.query.filter_by(character_id=character.id, name=item_name).first()
                    if existing:
                        existing.quantity = (existing.quantity or 0) + 1
                    else:
                        db.session.add(CharacterEquipment(character=character, name=item_name, quantity=1))
        else:
            config = SHEET_SCALAR_FIELDS.get(field)
            if not config:
                continue
            setattr(character, field, bool(after) if config['type'] == 'bool' else int(after))

    character.updated_at = datetime.utcnow()
    proposal.status = 'applied'
    proposal.applied_at = datetime.utcnow()
    db.session.commit()
    return character


@automation_bp.route('/api/automation', methods=['GET'])
@token_required
def get_automation_workspace(current_user):
    worker_id = request.args.get('worker_id')
    api_base = request.args.get('api_base')
    if worker_id:
        record_worker_activity(worker_id, api_base=api_base, is_heartbeat=False)
    return jsonify(_workspace_payload(current_user)), 200


@automation_bp.route('/api/automation/stream', methods=['GET'])
def stream_automation_workspace():
    current_user, error_response = _auth_stream_user()
    if error_response is not None:
        return error_response

    last_event_id = _last_event_id()

    def event_stream():
        if last_event_id:
            cursor = int(last_event_id)
        else:
            workspace = _workspace_payload(current_user)
            yield sse_message({'type': 'bootstrap', 'workspace': workspace})
            cursor = workspace_stream_cursor()
        for row in iter_workspace_events(cursor):
            if row is None:
                yield ': ping\n\n'
                continue
            yield sse_message(row.payload_json or {'type': row.event_type}, event_id=row.id)

    return _stream_response(event_stream())


@automation_bp.route('/api/automation/scorecards', methods=['GET'])
@token_required
def list_automation_scorecards(current_user):
    rows = AutomationScorecardTemplate.query.order_by(AutomationScorecardTemplate.updated_at.desc(), AutomationScorecardTemplate.id.desc()).all()
    return jsonify({'scorecards': [row.to_dict() for row in rows]}), 200


@automation_bp.route('/api/automation/scorecards', methods=['POST'])
@token_required
def create_automation_scorecard(current_user):
    data = request.get_json(silent=True) or {}
    try:
        normalized = validate_scorecard_template_payload(data)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    row = AutomationScorecardTemplate(
        user_id=current_user.id,
        name=normalized['name'],
        description=normalized['description'],
        instructions=normalized['instructions'],
        criteria_json=normalized['criteria'],
        defaults_json=normalized['defaults'],
    )
    db.session.add(row)
    db.session.commit()
    append_workspace_event(
        current_user.id,
        'scorecard_created',
        {'type': 'scorecard_created', 'scorecard': row.to_dict()},
        resource_type='scorecard',
        resource_id=row.id,
    )
    return jsonify({'scorecard': row.to_dict()}), 201


@automation_bp.route('/api/automation/scorecards/<int:scorecard_id>', methods=['GET'])
@token_required
def get_automation_scorecard(current_user, scorecard_id):
    row = get_or_404(AutomationScorecardTemplate, scorecard_id)
    if not _scorecard_visible_to_user(current_user, row):
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify({'scorecard': row.to_dict()}), 200


@automation_bp.route('/api/automation/scorecards/<int:scorecard_id>', methods=['PUT'])
@token_required
def update_automation_scorecard(current_user, scorecard_id):
    row = get_or_404(AutomationScorecardTemplate, scorecard_id)
    if not _scorecard_owned_by_user(current_user, row):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    payload = {
        'name': data.get('name', row.name),
        'description': data.get('description', row.description),
        'instructions': data.get('instructions', row.instructions),
        'criteria': data.get('criteria', row.criteria_json),
        'defaults': data.get('defaults', row.defaults_json),
    }
    try:
        normalized = validate_scorecard_template_payload(payload)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    row.name = normalized['name']
    row.description = normalized['description']
    row.instructions = normalized['instructions']
    row.criteria_json = normalized['criteria']
    row.defaults_json = normalized['defaults']
    db.session.commit()
    append_workspace_event(
        current_user.id,
        'scorecard_updated',
        {'type': 'scorecard_updated', 'scorecard': row.to_dict()},
        resource_type='scorecard',
        resource_id=row.id,
    )
    return jsonify({'scorecard': row.to_dict()}), 200


@automation_bp.route('/api/automation/scorecards/<int:scorecard_id>', methods=['DELETE'])
@token_required
def delete_automation_scorecard(current_user, scorecard_id):
    row = get_or_404(AutomationScorecardTemplate, scorecard_id)
    if not _scorecard_owned_by_user(current_user, row):
        return jsonify({'error': 'Forbidden'}), 403
    AutomationScenario.query.filter_by(scorecard_template_id=row.id).update({'scorecard_template_id': None}, synchronize_session=False)
    db.session.delete(row)
    db.session.commit()
    append_workspace_event(
        current_user.id,
        'scorecard_deleted',
        {'type': 'scorecard_deleted', 'scorecard_id': scorecard_id},
        resource_type='scorecard',
        resource_id=scorecard_id,
    )
    return jsonify({'ok': True}), 200


@automation_bp.route('/api/automation/scenarios', methods=['GET'])
@token_required
def list_automation_scenarios(current_user):
    scenarios = AutomationScenario.query.order_by(AutomationScenario.updated_at.desc(), AutomationScenario.id.desc()).all()
    return jsonify({'scenarios': [scenario.to_dict() for scenario in scenarios]}), 200


@automation_bp.route('/api/automation/scenarios', methods=['POST'])
@token_required
def create_automation_scenario(current_user):
    data = request.get_json(silent=True) or {}
    source_campaign_id = data.get('source_campaign_id')
    if not source_campaign_id:
        return jsonify({'error': 'source_campaign_id is required'}), 400

    campaign = get_or_404(Campaign, source_campaign_id)
    if campaign.is_automation_clone or not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    scorecard_template = None
    if data.get('scorecard_template_id') is not None:
        scorecard_template = get_or_404(AutomationScorecardTemplate, data.get('scorecard_template_id'))
        if scorecard_template.user_id != current_user.id:
            return jsonify({'error': 'Forbidden'}), 403

    scenario = AutomationScenario(
        user_id=current_user.id,
        source_campaign_id=campaign.id,
        scorecard_template_id=scorecard_template.id if scorecard_template else None,
        name=(data.get('name') or f'{campaign.name} benchmark').strip(),
        description=data.get('description'),
        runner_config_json=merge_auditor_config_into_runner_config(
            runner_config_from_request(data.get('runner_config'), data.get('audit_config'))
        ),
        audit_config_json=data.get('audit_config') or {},
        retention_policy_json=data.get('retention_policy') or {},
        roster_json=scenario_roster_from_campaign(campaign),
    )
    db.session.add(scenario)
    db.session.commit()
    append_workspace_event(
        current_user.id,
        'scenario_created',
        {'type': 'scenario_created', 'scenario': scenario.to_dict()},
        resource_type='scenario',
        resource_id=scenario.id,
    )
    return jsonify({'scenario': scenario.to_dict()}), 201


@automation_bp.route('/api/automation/scenarios/<int:scenario_id>', methods=['GET'])
@token_required
def get_automation_scenario(current_user, scenario_id):
    scenario = get_or_404(AutomationScenario, scenario_id)
    if not _scenario_visible_to_user(current_user, scenario):
        return jsonify({'error': 'Forbidden'}), 403

    snapshots = AutomationSnapshot.query.filter_by(scenario_id=scenario.id).order_by(AutomationSnapshot.created_at.desc(), AutomationSnapshot.id.desc()).all()
    runs = AutomationRun.query.filter_by(scenario_id=scenario.id).order_by(AutomationRun.created_at.desc(), AutomationRun.id.desc()).all()
    return jsonify({
        'scenario': scenario.to_dict(),
        'baseline_run': baseline_run_for_scenario(scenario).to_dict() if baseline_run_for_scenario(scenario) else None,
        'snapshots': [snapshot.to_dict() for snapshot in snapshots],
        'runs': [run.to_dict() for run in runs],
        'viewer_permissions': _scenario_viewer_permissions(current_user, scenario),
    }), 200


@automation_bp.route('/api/automation/scenarios/<int:scenario_id>', methods=['PUT'])
@token_required
def update_automation_scenario(current_user, scenario_id):
    scenario = get_or_404(AutomationScenario, scenario_id)
    if not _scenario_owned_by_user(current_user, scenario):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    if 'name' in data:
        scenario.name = (data.get('name') or '').strip() or scenario.name
    if 'description' in data:
        scenario.description = data.get('description')
    if 'runner_config' in data or 'audit_config' in data:
        next_runner_config = data.get('runner_config') if 'runner_config' in data else scenario.runner_config_json
        next_audit_config = data.get('audit_config') if 'audit_config' in data else scenario.audit_config_json
        scenario.runner_config_json = merge_auditor_config_into_runner_config(
            runner_config_from_request(next_runner_config, next_audit_config)
        )
        if 'audit_config' in data:
            scenario.audit_config_json = data.get('audit_config') or {}
    if 'retention_policy' in data:
        scenario.retention_policy_json = data.get('retention_policy') or {}
    if 'scorecard_template_id' in data:
        scorecard_template_id = data.get('scorecard_template_id')
        if scorecard_template_id is None:
            scenario.scorecard_template_id = None
        else:
            scorecard_template = get_or_404(AutomationScorecardTemplate, scorecard_template_id)
            if scorecard_template.user_id != current_user.id:
                return jsonify({'error': 'Forbidden'}), 403
            scenario.scorecard_template_id = scorecard_template.id
    if 'baseline_run_id' in data:
        baseline_run_id = data.get('baseline_run_id')
        if baseline_run_id is None:
            scenario.baseline_run_id = None
        else:
            baseline_run = get_or_404(AutomationRun, baseline_run_id)
            if baseline_run.scenario_id != scenario.id:
                return jsonify({'error': 'Baseline run must belong to the same scenario'}), 400
            scenario.baseline_run_id = baseline_run.id
    scenario.updated_at = datetime.utcnow()
    db.session.commit()
    append_workspace_event(
        current_user.id,
        'scenario_updated',
        {'type': 'scenario_updated', 'scenario': scenario.to_dict()},
        resource_type='scenario',
        resource_id=scenario.id,
    )
    return jsonify({'scenario': scenario.to_dict()}), 200


@automation_bp.route('/api/automation/scenarios/<int:scenario_id>', methods=['DELETE'])
@token_required
def delete_automation_scenario(current_user, scenario_id):
    scenario = get_or_404(AutomationScenario, scenario_id)
    if not _scenario_owned_by_user(current_user, scenario):
        return jsonify({'error': 'Forbidden'}), 403
    run_ids = [run.id for run in AutomationRun.query.filter_by(scenario_id=scenario.id).all()]
    if run_ids:
        AutomationRunAuditorJob.query.filter(AutomationRunAuditorJob.run_id.in_(run_ids)).delete(synchronize_session=False)
        AutomationRunAuditCycle.query.filter(AutomationRunAuditCycle.run_id.in_(run_ids)).delete(synchronize_session=False)
        AutomationRunProviderCall.query.filter(AutomationRunProviderCall.run_id.in_(run_ids)).delete(synchronize_session=False)
        AutomationRunAuditResult.query.filter(AutomationRunAuditResult.run_id.in_(run_ids)).delete(synchronize_session=False)
        AutomationRunEvent.query.filter(AutomationRunEvent.run_id.in_(run_ids)).delete(synchronize_session=False)
        AutomationRun.query.filter(AutomationRun.id.in_(run_ids)).delete(synchronize_session=False)
    AutomationSnapshot.query.filter_by(scenario_id=scenario.id).delete(synchronize_session=False)
    db.session.delete(scenario)
    db.session.commit()
    append_workspace_event(
        current_user.id,
        'scenario_deleted',
        {'type': 'scenario_deleted', 'scenario_id': scenario_id},
        resource_type='scenario',
        resource_id=scenario_id,
    )
    return jsonify({'ok': True}), 200


@automation_bp.route('/api/automation/scenarios/<int:scenario_id>/snapshots', methods=['GET'])
@token_required
def list_automation_snapshots(current_user, scenario_id):
    scenario = get_or_404(AutomationScenario, scenario_id)
    if not _scenario_visible_to_user(current_user, scenario):
        return jsonify({'error': 'Forbidden'}), 403
    snapshots = AutomationSnapshot.query.filter_by(scenario_id=scenario.id).order_by(AutomationSnapshot.created_at.desc(), AutomationSnapshot.id.desc()).all()
    return jsonify({'snapshots': [snapshot.to_dict() for snapshot in snapshots]}), 200


@automation_bp.route('/api/automation/scenarios/<int:scenario_id>/snapshots', methods=['POST'])
@token_required
def create_automation_snapshot(current_user, scenario_id):
    scenario = get_or_404(AutomationScenario, scenario_id)
    if not _scenario_owned_by_user(current_user, scenario):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json(silent=True) or {}
    snapshot = create_snapshot_for_scenario(
        scenario,
        label=data.get('label'),
        summary=data.get('summary'),
        source_session_id=data.get('source_session_id'),
    )
    append_workspace_event(
        current_user.id,
        'snapshot_created',
        {'type': 'snapshot_created', 'scenario_id': scenario.id, 'snapshot': snapshot.to_dict()},
        resource_type='snapshot',
        resource_id=snapshot.id,
    )
    return jsonify({'snapshot': snapshot.to_dict()}), 201


@automation_bp.route('/api/automation/scenarios/<int:scenario_id>/cleanup', methods=['POST'])
@token_required
def cleanup_automation_scenario(current_user, scenario_id):
    scenario = get_or_404(AutomationScenario, scenario_id)
    if not _scenario_owned_by_user(current_user, scenario):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json(silent=True) or {}
    result = cleanup_hidden_clone_campaigns(
        scenario,
        action=data.get('action'),
        older_than_days=data.get('older_than_days'),
        keep_recent_runs=data.get('keep_recent_runs'),
    )
    append_workspace_event(
        current_user.id,
        'scenario_cleanup',
        {'type': 'scenario_cleanup', 'scenario_id': scenario.id, **result},
        resource_type='scenario',
        resource_id=scenario.id,
    )
    return jsonify(result), 200


@automation_bp.route('/api/automation/scenarios/<int:scenario_id>/runs', methods=['GET'])
@token_required
def list_automation_runs(current_user, scenario_id):
    scenario = get_or_404(AutomationScenario, scenario_id)
    if not _scenario_visible_to_user(current_user, scenario):
        return jsonify({'error': 'Forbidden'}), 403
    runs = AutomationRun.query.filter_by(scenario_id=scenario.id).order_by(AutomationRun.created_at.desc(), AutomationRun.id.desc()).all()
    return jsonify({'runs': [run.to_dict() for run in runs]}), 200


@automation_bp.route('/api/automation/scenarios/<int:scenario_id>/runs', methods=['POST'])
@token_required
def create_automation_run(current_user, scenario_id):
    scenario = get_or_404(AutomationScenario, scenario_id)
    if not _scenario_owned_by_user(current_user, scenario):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    snapshot_id = data.get('snapshot_id')
    if not snapshot_id:
        latest = AutomationSnapshot.query.filter_by(scenario_id=scenario.id).order_by(AutomationSnapshot.created_at.desc(), AutomationSnapshot.id.desc()).first()
        if not latest:
            return jsonify({'error': 'snapshot_id is required when the scenario has no snapshots'}), 400
        snapshot = latest
    else:
        snapshot = get_or_404(AutomationSnapshot, snapshot_id)
        if snapshot.scenario_id != scenario.id:
            return jsonify({'error': 'Snapshot does not belong to this scenario'}), 400

    matrix = data.get('matrix')
    if isinstance(matrix, list) and matrix:
        normalized_matrix = []
        for entry in matrix:
            entry = entry if isinstance(entry, dict) else {}
            normalized_matrix.append({
                **entry,
                'runner_config': merge_auditor_config_into_runner_config(
                    runner_config_from_request(entry.get('runner_config'), entry.get('audit_config'), entry)
                ),
            })
        group_id, runs = create_matrix_runs(scenario, snapshot, current_user, normalized_matrix)
        return jsonify({'group_id': group_id, 'runs': [run.to_dict() for run in runs]}), 201

    merged_runner_config = merge_auditor_config_into_runner_config(
        merged_runner_config_for_scenario(
            scenario,
            runner_config_from_request(data.get('runner_config'), data.get('audit_config'), data),
        )
    )
    run = AutomationRun(
        scenario_id=scenario.id,
        snapshot_id=snapshot.id,
        user_id=current_user.id,
        status='queued',
        scorecard_template_json=scorecard_template_snapshot(scenario.scorecard_template),
        runner_config_json=merged_runner_config,
    )
    db.session.add(run)
    db.session.commit()
    append_run_event(run, 'run_queued', {'status': run.status}, dedupe_key=f'run_queued:{run.id}')
    return jsonify({'run': run.to_dict()}), 201


@automation_bp.route('/api/automation/runs/<int:run_id>', methods=['GET'])
@token_required
def get_automation_run(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_visible_to_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify(run_watch_payload(run, current_user=current_user)), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/stream', methods=['GET'])
def stream_automation_run(run_id):
    current_user, error_response = _auth_stream_user()
    if error_response is not None:
        return error_response

    run = get_or_404(AutomationRun, run_id)
    if not _run_visible_to_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403

    last_event_id = _last_event_id()

    def event_stream():
        if last_event_id:
            cursor = int(last_event_id)
        else:
            yield sse_message({'type': 'bootstrap', 'run': run_watch_payload(run, current_user=current_user)})
            cursor = run_stream_cursor(run.id)
        for row in iter_run_events(run.id, cursor):
            if row is None:
                yield ': ping\n\n'
                continue
            current_run = db.session.get(AutomationRun, run.id)
            payload = row.payload_json or {}
            delta = {}
            if row.event_type == 'player_message_posted' and isinstance(payload.get('message'), dict):
                delta['message'] = payload.get('message')
            if row.event_type == 'dm_turn_status' and payload.get('dm_message_id'):
                dm_message = db.session.get(SessionMessage, payload.get('dm_message_id'))
                if dm_message is not None:
                    delta['message'] = dm_message.to_dict()
            latest_session = latest_session_for_run(current_run)
            if latest_session and row.event_type in {'player_decision_applied', 'dm_turn_status'}:
                delta['pending_sheet_proposals'] = [
                    proposal.to_dict()
                    for proposal in SheetProposal.query.filter_by(session_id=latest_session.id, status='pending')
                    .order_by(SheetProposal.created_at.asc(), SheetProposal.id.asc())
                    .all()
                ]
            if row.event_type in {
                'audit_cycle_paused',
                'audit_cycle_audited',
                'audit_cycle_continued',
                'run_scorecard_updated',
                'auditor_jobs_updated',
                'auditor_job_started',
                'auditor_job_completed',
                'auditor_job_failed',
                'auditor_target_cycles_reached',
            }:
                audit_cycles = [
                    cycle.to_dict()
                    for cycle in AutomationRunAuditCycle.query.filter_by(run_id=current_run.id)
                    .order_by(AutomationRunAuditCycle.cycle_number.asc(), AutomationRunAuditCycle.id.asc())
                    .all()
                ]
                delta['audit_cycles'] = audit_cycles
                delta['current_audit_cycle'] = next((cycle for cycle in audit_cycles if cycle['id'] == current_run.awaiting_audit_cycle_id), None)
                delta['auditor_jobs'] = [
                    job.to_dict()
                    for job in AutomationRunAuditorJob.query.filter_by(run_id=current_run.id)
                    .order_by(AutomationRunAuditorJob.cycle_id.asc(), AutomationRunAuditorJob.auditor_slot.asc(), AutomationRunAuditorJob.id.asc())
                    .all()
                ]
            if row.event_type == 'run_scorecard_updated':
                delta['scorecard'] = payload.get('scorecard') or []
            yield sse_message({
                'type': 'run_event',
                'run_id': run.id,
                'run': current_run.to_dict(),
                'event': row.to_dict(),
                'delta': delta,
                'incidents': (current_run.scorecard_summary_json or {}).get('incidents') or [],
                'baseline_comparison': current_run.baseline_comparison_json or {},
                'scorecard_template': current_run.scorecard_template_json or {},
                'viewer_permissions': _run_viewer_permissions(current_user, current_run),
            }, event_id=row.id)

    return _stream_response(event_stream())


@automation_bp.route('/api/automation/runs/<int:run_id>/stop', methods=['POST'])
@token_required
def stop_automation_run(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_owned_by_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403

    if run.status in {'completed', 'failed', 'stopped'}:
        return jsonify({'run': run.to_dict()}), 200

    if run.status == 'queued':
        run.status = 'stopped'
        run.finished_at = datetime.utcnow()
    else:
        run.status = 'stop_requested'
        run.stop_requested_at = datetime.utcnow()
    db.session.commit()
    append_run_event(run, 'run_stop_requested', {'status': run.status}, dedupe_key=f'run_stop_requested:{run.id}:{run.status}')
    return jsonify({'run': run.to_dict()}), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/claim', methods=['POST'])
@token_required
def claim_automation_run(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_owned_by_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    worker_id = data.get('worker_id') or f'worker-{current_user.id}'
    api_base = data.get('api_base')

    run.last_claim_attempt_at = datetime.utcnow()
    db.session.commit()

    if worker_id:
        record_worker_activity(worker_id, api_base=api_base, is_heartbeat=False)

    try:
        claim_data = claim_run_for_worker(run, worker_id)
        run.claim_failure_reason = None
        if api_base:
            run.worker_api_base = api_base
        db.session.commit()
    except CloneRetrievalPreflightError as exc:
        db.session.rollback()
        run = db.session.get(AutomationRun, run_id)
        run.claim_failure_reason = str(exc)
        db.session.commit()
        return jsonify({
            'error': str(exc),
            'retrieval_preflight': exc.report,
        }), 409
    except ValueError as exc:
        db.session.rollback()
        run = db.session.get(AutomationRun, run_id)
        run.claim_failure_reason = str(exc)
        db.session.commit()
        return jsonify({'error': str(exc)}), 409

    append_run_event(
        run,
        'run_claimed',
        {
            'status': run.status,
            'worker_id': run.worker_id,
            'has_lease_token': bool(run.lease_token),
            'lease_expires_at': run.lease_expires_at.isoformat() if run.lease_expires_at else None,
            'derived_campaign_id': run.derived_campaign_id,
            'reclaimed': claim_data['reclaimed'],
            'attempt_count': run.attempt_count,
            'retrieval_preflight': claim_data['retrieval_preflight'],
        },
        dedupe_key=f'run_claimed:{run.id}:attempt:{run.attempt_count}',
    )

    roster = []
    for entry in run.scenario.roster_json or []:
        roster.append({
            **entry,
            'derived_character_id': claim_data['character_map'].get(entry.get('character_id')),
        })
    return jsonify({
        'run': run.to_dict(),
        'scenario': run.scenario.to_dict() if run.scenario else None,
        'snapshot': run.snapshot.to_dict() if run.snapshot else None,
        'derived_campaign': claim_data['clone_campaign'].to_dict(),
        'roster': roster,
        'latest_session': claim_data['latest_session'].to_dict() if claim_data['latest_session'] else None,
        'lease_token': run.lease_token,
        'reclaimed': claim_data['reclaimed'],
        'retrieval_preflight': claim_data['retrieval_preflight'],
    }), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/heartbeat', methods=['POST'])
@token_required
def heartbeat_automation_run(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_owned_by_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    worker_id = data.get('worker_id')
    api_base = data.get('api_base')
    if worker_id:
        record_worker_activity(worker_id, api_base=api_base, is_heartbeat=True)

    try:
        heartbeat_run(
            run,
            worker_id=worker_id,
            lease_token=data.get('lease_token'),
        )
        if api_base:
            run.worker_api_base = api_base
            db.session.commit()
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    return jsonify({'run': run.to_dict()}), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/pause', methods=['POST'])
@token_required
def pause_automation_run(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_owned_by_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    phase = (data.get('phase') or '').strip().lower()
    if phase not in {'after_player', 'after_dm'}:
        return jsonify({'error': 'phase must be after_player or after_dm'}), 400
    try:
        ensure_worker_lease(run, worker_id=data.get('worker_id'), lease_token=data.get('lease_token'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409

    player_message_id = data.get('player_message_id')
    dm_message_id = data.get('dm_message_id')

    # Idempotency check
    existing = None
    if phase == 'after_player' and player_message_id is not None:
        existing = AutomationRunAuditCycle.query.filter_by(
            run_id=run.id, phase='after_player', player_message_id=player_message_id
        ).first()
    elif phase == 'after_dm':
        if dm_message_id is not None:
            existing = AutomationRunAuditCycle.query.filter_by(
                run_id=run.id, phase='after_dm', dm_message_id=dm_message_id
            ).first()
        if not existing and player_message_id is not None:
            existing = AutomationRunAuditCycle.query.filter_by(
                run_id=run.id, phase='after_dm', player_message_id=player_message_id
            ).first()

    if existing:
        if existing.status == 'pending':
            run.status = 'awaiting_audit'
            run.awaiting_audit_cycle_id = existing.id
            run.awaiting_audit_phase = phase
            run.updated_at = datetime.utcnow()
            db.session.commit()
            return jsonify({
                'run': run.to_dict(),
                'audit_cycle': existing.to_dict(),
                'paused': True,
                'created': False,
            }), 200
        return jsonify({
            'run': run.to_dict(),
            'audit_cycle': existing.to_dict(),
            'paused': False,
            'created': False,
        }), 200

    if run.awaiting_audit_cycle_id and run.status == 'awaiting_audit':
        cycle = db.session.get(AutomationRunAuditCycle, run.awaiting_audit_cycle_id)
        if cycle and cycle.phase == phase:
            matches_msg = True
            if phase == 'after_player' and player_message_id is not None:
                matches_msg = (cycle.player_message_id == player_message_id)
            elif phase == 'after_dm':
                if dm_message_id is not None:
                    matches_msg = (cycle.dm_message_id == dm_message_id)
                elif player_message_id is not None:
                    matches_msg = (cycle.player_message_id == player_message_id)
            if matches_msg:
                return jsonify({'run': run.to_dict(), 'audit_cycle': cycle.to_dict(), 'paused': True}), 200
        return jsonify({'error': 'Run is already awaiting a different audit cycle'}), 409

    cycle = create_audit_cycle(
        run,
        phase,
        payload=data.get('payload') or {},
        summary=data.get('summary'),
        player_message_id=data.get('player_message_id'),
        dm_message_id=data.get('dm_message_id'),
        dedupe_key=data.get('dedupe_key'),
        worker_id=data.get('worker_id'),
        lease_token=data.get('lease_token'),
    )
    return jsonify({'run': run.to_dict(), 'audit_cycle': cycle.to_dict(), 'paused': True}), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/audit-cycles/<int:cycle_id>/audit', methods=['POST'])
@token_required
def audit_automation_run_cycle(current_user, run_id, cycle_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_owned_by_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403
    cycle = get_or_404(AutomationRunAuditCycle, cycle_id)
    if cycle.run_id != run.id:
        return jsonify({'error': 'Audit cycle does not belong to this run'}), 400

    data = request.get_json(silent=True) or {}

    # Sanitize auditor_job_id to ensure it exists for this run
    job_id = None
    raw_job_id = data.get('auditor_job_id')
    if raw_job_id:
        job = AutomationRunAuditorJob.query.filter_by(id=raw_job_id, run_id=run.id).first()
        if job:
            job_id = job.id

    # Snapshot context for logging attempts safely
    attempt_context = {
        "run_id": run.id,
        "cycle_id": cycle.id,
        "cycle_number": cycle.cycle_number,
        "phase": cycle.phase,
        "auditor_job_id": job_id,
        "auditor_slot": data.get('auditor_slot'),
        "provider": data.get('provider'),
        "model": data.get('model'),
        "raw_payload_json": data,
    }

    try:
        cycle = submit_audit_cycle_feedback(
            cycle,
            summary=data.get('summary'),
            notes=data.get('notes'),
            scorecard=data.get('scorecard'),
        )
        
        # Log successful attempt
        attempt = AutomationRunAuditAttempt(
            status="success",
            normalized_payload_json={
                "summary": cycle.summary,
                "notes": cycle.notes,
                "scorecard": cycle.scorecard_json,
            },
            **attempt_context
        )
        db.session.add(attempt)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        
        # Log failed attempt in clean transaction
        attempt = AutomationRunAuditAttempt(
            status="failed",
            error_class=e.__class__.__name__,
            error_message=str(e),
            **attempt_context
        )
        db.session.add(attempt)
        db.session.commit()
        raise

    append_run_event(
        run,
        'audit_cycle_audited',
        {'audit_cycle': cycle.to_dict()},
        dedupe_key=data.get('dedupe_key') or f'audit_cycle_audited:{cycle.id}:{cycle.updated_at.isoformat() if cycle.updated_at else cycle.id}',
    )
    scorecard = refresh_run_scorecard(run)
    append_run_event(
        run,
        'run_scorecard_updated',
        {
            'scorecard': scorecard,
            'scorecard_summary': run.scorecard_summary_json or {},
            'baseline_comparison': run.baseline_comparison_json or {},
        },
        dedupe_key=f'run_scorecard_updated:{run.id}:{run.last_event_sequence or 0}',
    )
    return jsonify({'run': run.to_dict(), 'audit_cycle': cycle.to_dict(), 'scorecard': scorecard}), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/continue', methods=['POST'])
@token_required
def continue_automation_run(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_owned_by_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json(silent=True) or {}
    try:
        cycle = continue_audit_run(run, force=bool(data.get('force')))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409

    append_run_event(
        run,
        'audit_cycle_continued',
        {'audit_cycle_id': cycle.id if cycle else None, 'force': bool(data.get('force'))},
        dedupe_key=data.get('dedupe_key') or f'audit_cycle_continued:{run.id}:{cycle.id if cycle else "none"}:{run.audit_resumed_at.isoformat() if run.audit_resumed_at else "now"}',
    )
    return jsonify({'run': run.to_dict(), 'audit_cycle': cycle.to_dict() if cycle else None}), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/auditors', methods=['GET'])
@token_required
def list_automation_run_auditors(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_visible_to_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403
    cycle_id = request.args.get('cycle_id', type=int)
    jobs = list_auditor_jobs(run.id, cycle_id)
    return jsonify({'run': run.to_dict(), 'auditor_jobs': [job.to_dict() for job in jobs]}), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/auditors/start', methods=['POST'])
@token_required
def start_automation_run_auditors(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_owned_by_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403
    if run.status != 'awaiting_audit' or not run.awaiting_audit_cycle_id:
        return jsonify({'error': 'Run is not awaiting an audit cycle'}), 409

    data = request.get_json(silent=True) or {}
    config = dict(run.runner_config_json or {})
    auditor_config = normalize_auditor_config({
        **((config.get('auditor_config') or {}) if isinstance(config.get('auditor_config'), dict) else {}),
        **((data.get('auditor_config') or {}) if isinstance(data.get('auditor_config'), dict) else {}),
    })
    if auditor_config.get('mode') != 'built_in':
        auditor_config['mode'] = 'built_in'
    config['auditor_config'] = auditor_config
    run.runner_config_json = config
    db.session.commit()

    cycle = db.session.get(AutomationRunAuditCycle, run.awaiting_audit_cycle_id)
    if cycle is None:
        return jsonify({'error': 'Current audit cycle was not found'}), 409
    jobs = ensure_auditor_jobs_for_cycle(run, cycle, auditor_config, rerun_failed=bool(data.get('rerun_failed')))
    append_run_event(
        run,
        'auditor_jobs_updated',
        {'auditor_jobs': [job.to_dict() for job in jobs], 'cycle_id': cycle.id},
        dedupe_key=f'auditor_jobs_requested:{run.id}:{cycle.id}:{datetime.utcnow().isoformat()}',
    )
    if data.get('sync'):
        try:
            result = run_builtin_auditors_for_current_cycle(run.id, rerun_failed=bool(data.get('rerun_failed')))
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 409
        return jsonify(result), 200

    app = current_app._get_current_object()
    thread = threading.Thread(target=_run_auditors_in_background, args=(app, run.id), daemon=True)
    thread.start()
    return jsonify({'run': run.to_dict(), 'auditor_jobs': [job.to_dict() for job in jobs], 'started': True}), 202


@automation_bp.route('/api/automation/runs/<int:run_id>/auditors/stop', methods=['POST'])
@token_required
def stop_automation_run_auditors(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_owned_by_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403
    jobs = cancel_auditor_jobs_for_current_cycle(run)
    return jsonify({'run': run.to_dict(), 'auditor_jobs': [job.to_dict() for job in jobs], 'stopped': True}), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/auditor-tools/<tool_name>', methods=['POST'])
@token_required
def execute_automation_auditor_tool(current_user, run_id, tool_name):
    run = get_or_404(AutomationRun, run_id)
    if not _run_visible_to_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403

    allowed_tools = {
        item.get('function', {}).get('name')
        for item in AUDITOR_TOOL_DEFINITIONS
        if item.get('function', {}).get('name')
    }
    if tool_name not in allowed_tools:
        return jsonify({'error': f'Unknown auditor tool: {tool_name}'}), 404

    data = request.get_json(silent=True) or {}
    args = data.get('args') if isinstance(data.get('args'), dict) else {}
    result = execute_auditor_tool(run, tool_name, args)
    return jsonify({'run_id': run.id, 'tool_name': tool_name, 'result': result}), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/player-repairs', methods=['POST'])
@token_required
def apply_automation_player_repair(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_owned_by_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    action = (data.get('action') or '').strip().lower()
    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({'error': 'reason is required for player-side repairs'}), 400

    before = None
    after = None
    character_id = data.get('character_id')
    try:
        if action == 'set_character_field':
            character = _automation_run_character(run, character_id)
            field = (data.get('field') or '').strip()
            value = _coerce_sheet_scalar_value(field, data.get('value'))
            before = character_full_dict(character)
            setattr(character, field, value)
            character.updated_at = datetime.utcnow()
            db.session.commit()
            after = character_full_dict(character)
        elif action == 'set_condition':
            character = _automation_run_character(run, character_id)
            condition_name = (data.get('condition_name') or '').strip()
            if not condition_name:
                return jsonify({'error': 'condition_name is required'}), 400
            before = character_full_dict(character)
            present = bool(data.get('present', True))
            existing = CharacterCondition.query.filter_by(
                character_id=character.id,
                condition_name=condition_name,
            ).first()
            if present:
                if existing is None:
                    existing = CharacterCondition(character_id=character.id, condition_name=condition_name)
                    db.session.add(existing)
                if 'description' in data:
                    existing.description = data.get('description')
                if 'source' in data:
                    existing.source = data.get('source')
                if 'is_permanent' in data:
                    existing.is_permanent = bool(data.get('is_permanent'))
                if 'duration_remaining' in data:
                    existing.duration_remaining = data.get('duration_remaining')
            elif existing is not None:
                db.session.delete(existing)
            character.updated_at = datetime.utcnow()
            db.session.commit()
            after = character_full_dict(character)
        elif action == 'adjust_equipment':
            character = _automation_run_character(run, character_id)
            item_name = (data.get('item_name') or data.get('name') or '').strip()
            if not item_name:
                return jsonify({'error': 'item_name is required'}), 400
            before = character_full_dict(character)
            existing = CharacterEquipment.query.filter_by(character_id=character.id, name=item_name).first()
            if existing is None:
                existing = CharacterEquipment(character_id=character.id, name=item_name, quantity=0)
                db.session.add(existing)
            if data.get('quantity') is not None:
                existing.quantity = max(0, int(data.get('quantity')))
            else:
                existing.quantity = max(0, int(existing.quantity or 0) + int(data.get('quantity_delta') or 0))
            for field in ('equipment_type', 'description', 'weight', 'is_equipped', 'armor_bonus', 'properties'):
                if field in data:
                    setattr(existing, field, data.get(field))
            if existing.quantity <= 0:
                db.session.delete(existing)
            character.updated_at = datetime.utcnow()
            db.session.commit()
            after = character_full_dict(character)
        elif action in {'apply_proposal', 'dismiss_proposal'}:
            session = latest_session_for_run(run)
            if session is None:
                return jsonify({'error': 'Run has no session to repair'}), 400
            proposal = db.session.get(SheetProposal, data.get('proposal_id'))
            if proposal is None or proposal.session_id != session.id or proposal.status != 'pending':
                return jsonify({'error': 'Pending proposal not found for this run'}), 400
            character = _automation_run_character(run, proposal.character_id)
            before = character_full_dict(character)
            if action == 'apply_proposal':
                character = _apply_proposal_direct(proposal)
            else:
                proposal.status = 'dismissed'
                db.session.commit()
            after = character_full_dict(character)
            character_id = character.id
        else:
            return jsonify({'error': 'action must be set_character_field, set_condition, adjust_equipment, apply_proposal, or dismiss_proposal'}), 400
    except (TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400

    repair_payload = {
        'repair_scope': 'player_side',
        'action': action,
        'reason': reason,
        'character_id': character_id,
        'exclude_from_dm_fault': bool(data.get('exclude_from_dm_fault', True)),
        'before': before,
        'after': after,
    }
    event_row, _created = append_run_event(
        run,
        'player_state_repaired',
        repair_payload,
        dedupe_key=data.get('dedupe_key'),
    )
    return jsonify({'run': run.to_dict(), 'event': event_row.to_dict(), 'repair': repair_payload}), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/events', methods=['POST'])
@token_required
def append_automation_run_event(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_owned_by_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    event_type = data.get('event_type')
    if not event_type:
        return jsonify({'error': 'event_type is required'}), 400

    if data.get('status'):
        run.status = data['status']
    if event_type in {'run_started', 'started'} and run.started_at is None:
        run.started_at = datetime.utcnow()
        run.status = 'running'
    if 'error_text' in data:
        run.error_text = data.get('error_text')
    if data.get('worker_id') or data.get('lease_token'):
        try:
            heartbeat_run(run, worker_id=data.get('worker_id'), lease_token=data.get('lease_token'))
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 409
    else:
        db.session.commit()

    try:
        event_row, created = append_run_event(
            run,
            event_type,
            data.get('payload') or {},
            dedupe_key=data.get('dedupe_key'),
            worker_id=data.get('worker_id'),
            lease_token=data.get('lease_token'),
        )
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    return jsonify({'event': event_row.to_dict(), 'run': run.to_dict(), 'created': created}), (201 if created else 200)


@automation_bp.route('/api/automation/runs/<int:run_id>/provider-calls', methods=['GET'])
@token_required
def get_automation_run_provider_calls(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_visible_to_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403
    include_artifacts = request.args.get('include_artifacts', 'false').lower() == 'true'
    rows = AutomationRunProviderCall.query.filter_by(run_id=run.id).order_by(AutomationRunProviderCall.created_at.asc(), AutomationRunProviderCall.id.asc()).all()
    return jsonify({'provider_calls': [row.to_dict(include_artifacts=include_artifacts) for row in rows]}), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/provider-calls', methods=['POST'])
@token_required
def create_automation_run_provider_call(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_owned_by_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json(silent=True) or {}
    if data.get('worker_id') or data.get('lease_token'):
        try:
            ensure_worker_lease(run, worker_id=data.get('worker_id'), lease_token=data.get('lease_token'))
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 409
    try:
        row, created = persist_provider_call(run, data)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'provider_call': row.to_dict(include_artifacts=True), 'created': created}), (201 if created else 200)


@automation_bp.route('/api/automation/runs/<int:run_id>/provider-calls/replay', methods=['GET'])
@token_required
def get_automation_run_provider_call_replay(current_user, run_id):
    source_run = get_or_404(AutomationRun, run_id)
    if not _run_visible_to_user(current_user, source_run):
        return jsonify({'error': 'Forbidden'}), 403
    dedupe_key = request.args.get('dedupe_key')
    if not dedupe_key:
        return jsonify({'error': 'dedupe_key is required'}), 400
    row = provider_call_for_replay(source_run.id, dedupe_key)
    if row is None:
        return jsonify({'error': 'Replay artifact not found'}), 404
    return jsonify({'provider_call': row.to_dict(include_artifacts=True)}), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/complete', methods=['POST'])
@token_required
def complete_automation_run(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_owned_by_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    if data.get('worker_id') or data.get('lease_token'):
        try:
            ensure_worker_lease(run, worker_id=data.get('worker_id'), lease_token=data.get('lease_token'))
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 409

    run.status = data.get('status') or 'completed'
    run.error_text = data.get('error_text')
    run.finished_at = datetime.utcnow()
    run.awaiting_audit_cycle_id = None
    run.awaiting_audit_phase = None
    if run.started_at is None:
        run.started_at = run.claimed_at or run.created_at
    run.heartbeat_at = datetime.utcnow()
    run.lease_expires_at = datetime.utcnow()
    db.session.commit()
    append_run_event(
        run,
        'run_completed',
        {'status': run.status, 'error_text': run.error_text},
        dedupe_key=data.get('dedupe_key') or f'run_completed:{run.id}:attempt:{run.attempt_count}',
        worker_id=data.get('worker_id'),
        lease_token=data.get('lease_token'),
    )
    checks = refresh_run_scorecard(run)
    append_run_event(
        run,
        'run_scorecard_updated',
        {
            'scorecard': checks,
            'scorecard_summary': run.scorecard_summary_json or {},
            'baseline_comparison': run.baseline_comparison_json or {},
        },
        dedupe_key=f'run_scorecard_updated:{run.id}:{run.last_event_sequence or 0}',
    )
    return jsonify({'run': run.to_dict(), 'scorecard': checks}), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/scorecard', methods=['GET'])
@token_required
def get_automation_run_scorecard(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_visible_to_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403
    refresh_run_scorecard(run)
    results = AutomationRunAuditResult.query.filter_by(run_id=run.id).order_by(AutomationRunAuditResult.check_id.asc()).all()
    return jsonify({'run': run.to_dict(), 'scorecard': [result.to_dict() for result in results], 'baseline_comparison': run.baseline_comparison_json or {}}), 200


@automation_bp.route('/api/automation/compare', methods=['POST'])
@token_required
def compare_automation_runs(current_user):
    data = request.get_json(silent=True) or {}
    left_run = get_or_404(AutomationRun, data.get('left_run_id'))
    right_run = get_or_404(AutomationRun, data.get('right_run_id'))
    if not _run_visible_to_user(current_user, left_run) or not _run_visible_to_user(current_user, right_run):
        return jsonify({'error': 'Forbidden'}), 403
    if left_run.scenario_id != right_run.scenario_id:
        return jsonify({'error': 'Runs must belong to the same scenario'}), 400
    return jsonify(compare_runs_payload(left_run, right_run)), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/decisions', methods=['POST'])
@token_required
def execute_automation_run_decision(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_owned_by_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403
    if not run.derived_campaign_id:
        return jsonify({'error': 'Run has no derived campaign'}), 400

    data = request.get_json(silent=True) or {}
    dedupe_key = data.get('dedupe_key')
    if dedupe_key:
        existing = AutomationRunEvent.query.filter_by(run_id=run.id, dedupe_key=dedupe_key).first()
        if existing:
            payload = existing.payload_json or {}
            if existing.event_type == 'player_message_posted':
                return jsonify({'message': payload.get('message')}), 200
            if existing.event_type == 'player_no_action':
                return jsonify({'ok': True}), 200
            if existing.event_type == 'player_decision_applied':
                return jsonify(payload), 200

    decision = data.get('decision') or {}
    action = (decision.get('action') or '').strip().lower()
    acting_user, acting_entry = _resolve_acting_user(run, data)
    if acting_user is None or acting_entry is None:
        return jsonify({'error': 'Could not resolve acting user'}), 400

    session = latest_session_for_run(run)
    if session is None:
        return jsonify({'error': 'Run has no session to act in'}), 400

    if action in {'apply_proposal', 'dismiss_proposal'}:
        proposal_id = decision.get('proposal_id')
        proposal = _pending_proposal_for_entry(session.id, acting_entry, proposal_id)
        if proposal is None:
            return jsonify({'error': f'Pending proposal {proposal_id} not found for this actor'}), 400
        if action == 'apply_proposal':
            character = _apply_proposal_direct(proposal)
            payload = {'proposal': proposal.to_dict(), 'character': character_full_dict(character)}
        else:
            proposal.status = 'dismissed'
            db.session.commit()
            payload = {'proposal': proposal.to_dict()}
        stream_manager.broadcast_event(session.id, {
            'type': 'proposal_applied' if action == 'apply_proposal' else 'proposal_dismissed',
            **payload,
        })
        append_run_event(run, 'player_decision_applied', {'action': action, **payload}, dedupe_key=dedupe_key)
        return jsonify(payload), 200

    if action == 'no_action':
        append_run_event(run, 'player_no_action', {'actor': acting_entry, 'decision': decision}, dedupe_key=dedupe_key)
        return jsonify({'ok': True}), 200

    content = decision.get('content')
    if not content:
        return jsonify({'error': 'Decision content is required for visible player actions'}), 400

    msg = SessionMessage(
        session_id=session.id,
        user_id=acting_user.id,
        role='player',
        content=content,
    )
    db.session.add(msg)
    db.session.commit()
    stream_manager.broadcast_event(session.id, {'type': 'message', 'message': msg.to_dict()})
    log_audit_event(
        run.derived_campaign_id,
        'player_input_stored',
        'Stored automation session player message.',
        {'session_id': session.id, 'message': msg.to_dict(), 'request_body': {'decision': decision}},
        source='session_messages',
        actor=acting_entry.get('label') or acting_user.username,
        commit=True,
    )
    stream_manager.start_generation(run.derived_campaign_id, session.id, acting_user.id, content, msg.id)
    append_run_event(run, 'player_message_posted', {'actor': acting_entry, 'message': msg.to_dict(), 'decision': decision}, dedupe_key=dedupe_key)
    return jsonify({'message': msg.to_dict()}), 201


@automation_bp.route('/api/automation/runs/<int:run_id>/audit-bundle', methods=['GET'])
@token_required
def get_automation_run_audit_bundle(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_visible_to_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403
    bundle = get_current_audit_bundle_data(run)
    return jsonify(bundle), 200


@automation_bp.route('/api/automation/runs/<int:run_id>/debug-summary', methods=['GET'])
@token_required
def get_automation_run_debug_summary_route(current_user, run_id):
    run = get_or_404(AutomationRun, run_id)
    if not _run_visible_to_user(current_user, run):
        return jsonify({'error': 'Forbidden'}), 403
    summary = run_debug_summary(run.id)
    return jsonify(summary), 200
