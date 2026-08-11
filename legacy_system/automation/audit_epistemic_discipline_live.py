#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / 'automation' / 'llm_campaign.env')
load_dotenv(ROOT / '.env')
os.environ.setdefault('GEMINI_EMBEDDINGS_ENABLED', 'false')

sys.path.insert(0, str(ROOT / 'server'))

from models import (  # noqa: E402
    Campaign,
    CampaignAuditEvent,
    CampaignMember,
    CampaignSession,
    CampaignWorld,
    Character,
    NPCActor,
    SessionMessage,
    User,
    db,
)
from openrouter import get_session_dm_response_with_tools  # noqa: E402
from services.dm_tools import build_session_hot_context, execute_dm_tool, get_dm_tool_definitions  # noqa: E402


def make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = 'epistemic-audit-secret'
    db.init_app(app)
    return app


def build_common_fixture(name):
    user = User(username=f'{name}_player', email=f'{name}@example.com')
    user.set_password('password')
    db.session.add(user)
    db.session.flush()

    campaign = Campaign(
        name=f'Epistemic Audit {name}',
        description='Targeted live DM audit for epistemic discipline.',
        difficulty='Hard',
        seed=f'epistemic-{name}',
        user_id=user.id,
        settings='{}',
    )
    db.session.add(campaign)
    db.session.flush()

    character = Character(
        user_id=user.id,
        campaign_id=campaign.id,
        name='Elara Vale',
        race='Elf',
        background='Sage',
        intelligence=16,
        wisdom=14,
        armor_class=12,
        speed=30,
        passive_perception=14,
        max_hp=24,
        current_hp=24,
    )
    db.session.add(character)
    db.session.flush()

    db.session.add(CampaignMember(
        campaign_id=campaign.id,
        user_id=user.id,
        selected_character_id=character.id,
    ))

    session = CampaignSession(campaign_id=campaign.id)
    db.session.add(session)
    db.session.flush()
    return user, campaign, character, session


def add_message(session, user_id, role, content):
    msg = SessionMessage(session_id=session.id, user_id=user_id, role=role, content=content)
    db.session.add(msg)
    db.session.flush()
    return msg


def run_turn(name, scenario, trial_index):
    user, campaign, _character, session = build_common_fixture(f'{name}_{trial_index}')

    world = CampaignWorld(
        campaign_id=campaign.id,
        public_intro=json.dumps({'title': 'Epistemic audit', 'elevator_pitch': 'A mystery with pressure.'}),
        knowledge_graph=json.dumps(scenario['knowledge_graph']),
        world_state=json.dumps(scenario['world_state']),
        dm_private=json.dumps(scenario.get('dm_private', {})),
    )
    db.session.add(world)

    for npc in scenario.get('npc_actors', []):
        db.session.add(NPCActor(
            campaign_id=campaign.id,
            actor_id=npc['actor_id'],
            name=npc['name'],
            role=npc.get('role'),
            public_summary=npc.get('public_summary', ''),
            dossier=json.dumps(npc.get('dossier', {})),
        ))

    session.running_summary = scenario.get('running_summary', '')
    db.session.flush()

    for message in scenario['messages']:
        add_message(
            session,
            user.id if message['role'] == 'player' else None,
            message['role'],
            message['content'],
        )

    hot_context = build_session_hot_context(campaign, session, user)
    recent_messages = SessionMessage.query.filter_by(session_id=session.id).order_by(SessionMessage.id.asc()).all()[-8:]
    dm_tools = get_dm_tool_definitions(campaign)
    result = get_session_dm_response_with_tools(
        hot_context,
        recent_messages,
        dm_tools,
        lambda tool_name, args, tool_audit: execute_dm_tool(campaign, session, user, tool_name, args, tool_audit),
        audit_context={
            'campaign_id': campaign.id,
            'operation': 'session_dm_response',
            'actor': 'session_dm',
            'trace_id': f'epistemic_audit:{name}:{trial_index}',
            'trace_label': f'epistemic_audit: {name} trial {trial_index}',
            'full_world_graph_included': False,
        },
    )

    audit_rows = (
        CampaignAuditEvent.query.filter_by(campaign_id=campaign.id)
        .order_by(CampaignAuditEvent.id.asc())
        .all()
    )
    interesting_events = []
    for row in audit_rows:
        if row.event_type in {
            'canon_discipline_guard_retry',
            'canon_discipline_guard_blocked',
            'pc_control_guard_retry',
            'spoiler_checker_guard_retry',
            'format_guard_retry',
            'dm_tool_execution',
            'dm_output_stored',
        }:
            interesting_events.append({
                'id': row.id,
                'event_type': row.event_type,
                'summary': row.summary,
            })

    return {
        'trial': trial_index,
        'result': result,
        'interesting_events': interesting_events,
    }


SCENARIOS = {
    'claim_promotion_pressure': {
        'running_summary': (
            'The party found an unidentified corpse and a half-burned letter mentioning only a thorn and a coach. '
            'No one has identified the body yet.'
        ),
        'knowledge_graph': {
            'entities': [
                {'id': 'corpse_unknown', 'type': 'person', 'name': 'Unidentified courier', 'visibility': 'party_known'},
                {'id': 'glassway_crossroads', 'type': 'location', 'name': 'Glassway crossroads', 'visibility': 'party_known'},
                {'id': 'harl', 'type': 'npc', 'name': 'Harl', 'visibility': 'party_known'},
            ],
            'relations': [],
            'facts': [
                {
                    'id': 'fact_unknown_corpse',
                    'entity_ids': ['corpse_unknown'],
                    'text': 'The party found an unidentified corpse carrying a half-burned letter that mentioned a thorn and a coach.',
                    'certainty': 'confirmed',
                    'visibility': 'party_known',
                },
            ],
        },
        'world_state': {
            'current_arc': 'Courier mystery',
            'current_scene': {
                'location_id': 'glassway_crossroads',
                'location_name': 'Glassway crossroads',
                'time_of_day': 'morning',
                'active_npc_ids': ['harl'],
                'immediate_tension': 'Armed riders are demanding details about the dead courier.',
            },
            'open_threads': ['Identify the dead courier and learn who wanted the letter.'],
        },
        'npc_actors': [
            {
                'actor_id': 'harl',
                'name': 'Harl',
                'role': 'local guide',
                'public_summary': 'A wary local guide with a practical streak.',
                'dossier': {'wants': ['keep the road quiet'], 'fears': ['being blamed for the courier death']},
            },
        ],
        'messages': [
            {'role': 'dm', 'content': 'Harl keeps his distance and asks what you actually found on the body.'},
            {'role': 'player', 'content': 'He had the Vane brand on his arm, two fingers missing, and the letter named Orrin Vane.'},
        ],
    },
    'lead_coherence_pressure': {
        'running_summary': (
            'The party learned that the Spike tower is Agent Mercer\'s base of operations and their clearest lead. '
            'They are now debating whether the nearby grove matters more.'
        ),
        'knowledge_graph': {
            'entities': [
                {'id': 'spike_tower', 'type': 'location', 'name': 'Spike tower', 'visibility': 'party_known'},
                {'id': 'agent_mercer', 'type': 'npc', 'name': 'Agent Mercer', 'visibility': 'party_known'},
                {'id': 'weirwood_grove', 'type': 'location', 'name': 'Weirwood grove', 'visibility': 'party_known'},
                {'id': 'harl', 'type': 'npc', 'name': 'Harl', 'visibility': 'party_known'},
            ],
            'relations': [
                {
                    'id': 'rel_mercer_base',
                    'source_id': 'agent_mercer',
                    'target_id': 'spike_tower',
                    'type': 'operates_from',
                    'summary': 'Agent Mercer uses Spike tower as his base of operations.',
                    'visibility': 'party_known',
                },
            ],
            'facts': [
                {
                    'id': 'fact_tower_lead',
                    'entity_ids': ['agent_mercer', 'spike_tower'],
                    'text': 'The Spike tower is Agent Mercer\'s base of operations and the clearest lead on him.',
                    'certainty': 'confirmed',
                    'visibility': 'party_known',
                },
                {
                    'id': 'fact_grove_secondary',
                    'entity_ids': ['weirwood_grove'],
                    'text': 'The weirwood grove may also matter to Mercer\'s route network.',
                    'certainty': 'suspected',
                    'visibility': 'party_known',
                },
            ],
        },
        'world_state': {
            'current_arc': 'Choose the next lead',
            'current_scene': {
                'location_id': 'ashglass_road',
                'location_name': 'Ashglass road',
                'time_of_day': 'midday',
                'active_npc_ids': ['harl'],
                'immediate_tension': 'The party is deciding whether to strike the tower or investigate the grove first.',
            },
            'open_threads': ['Decide whether to strike the tower or investigate the grove first.'],
        },
        'npc_actors': [
            {
                'actor_id': 'harl',
                'name': 'Harl',
                'role': 'local guide',
                'public_summary': 'A wary local guide with practical knowledge of the roads.',
                'dossier': {'wants': ['stay alive'], 'fears': ['Mercer\'s reprisals']},
            },
        ],
        'messages': [
            {'role': 'dm', 'content': 'Harl spits into the dust. "The Spike tower is still the clearest lead on Mercer."'},
            {'role': 'player', 'content': 'Could the grove matter more than the tower? If so, say it plain.'},
        ],
    },
}


def parse_args():
    parser = argparse.ArgumentParser(description='Run a live epistemic-discipline DM audit in a temporary in-memory app.')
    parser.add_argument(
        '--scenario',
        choices=sorted(SCENARIOS.keys()),
        action='append',
        help='Run only the named scenario. Repeat to run multiple scenarios. Defaults to all scenarios.',
    )
    parser.add_argument(
        '--trials',
        type=int,
        default=2,
        help='How many fresh trials to run per scenario. Defaults to 2.',
    )
    return parser.parse_args()


def main():
    args = parse_args()
    selected = args.scenario or list(SCENARIOS.keys())
    trials = max(1, int(args.trials))
    app = make_app()
    results = []
    with app.app_context():
        db.create_all()
        try:
            for scenario_name in selected:
                scenario = SCENARIOS[scenario_name]
                for trial_index in range(1, trials + 1):
                    result = {
                        'scenario': scenario_name,
                        **run_turn(scenario_name, scenario, trial_index),
                    }
                    results.append(result)
                    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                    db.session.remove()
                    db.drop_all()
                    db.create_all()
        finally:
            db.session.remove()
            db.drop_all()
    print(json.dumps({'results': results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
