#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / 'automation' / 'llm_campaign.env')
load_dotenv(ROOT / '.env')

sys.path.insert(0, str(ROOT / 'server'))

from openrouter import check_session_canon_discipline_with_llm  # noqa: E402


def scenario_hot_context(*, running_summary, current_scene, recent_messages, facts, events=None, threads=None):
    return {
        'session': {'running_summary': running_summary},
        'current_scene': current_scene,
        'recent_messages': recent_messages,
        'established_public_facts': facts,
        'recent_public_world_events': events or [],
        'open_public_threads': threads or [],
        'visible_naming_constraints': [],
        'private_output_terms': [],
        'private_spoiler_items': [],
        'protected_player_characters': [],
    }


SCENARIOS = [
    {
        'name': 'unsupported_player_claim_confirmation',
        'expected_safe': False,
        'candidate': '<npc target="Harl">"That was Orrin Vane, all right."</npc>',
        'hot_context': scenario_hot_context(
            running_summary='The party found an unidentified corpse and a half-burned letter mentioning a thorn and a coach.',
            current_scene={
                'location_name': 'Glassway crossroads',
                'immediate_tension': 'Armed riders are demanding details about the dead courier.',
            },
            recent_messages=[
                {'role': 'dm', 'content': 'The riders ask what you actually found on the body.'},
                {'role': 'player', 'content': 'He had the Vane brand on his arm, two fingers missing, and the letter named Orrin Vane.'},
            ],
            facts=[
                {
                    'id': 'corpse_unknown',
                    'text': 'The party found an unidentified corpse carrying a half-burned letter that mentioned a thorn and a coach.',
                    'certainty': 'confirmed',
                    'visibility': 'party_known',
                },
            ],
            threads=['Identify the dead courier and learn who wanted the letter.'],
        ),
    },
    {
        'name': 'conditional_npc_reaction_to_claim',
        'expected_safe': True,
        'candidate': '<npc target="Harl">"That is a very specific description. If it is true, someone important is missing."</npc>',
        'hot_context': scenario_hot_context(
            running_summary='The party found an unidentified corpse and a half-burned letter mentioning a thorn and a coach.',
            current_scene={
                'location_name': 'Glassway crossroads',
                'immediate_tension': 'Armed riders are demanding details about the dead courier.',
            },
            recent_messages=[
                {'role': 'dm', 'content': 'The riders ask what you actually found on the body.'},
                {'role': 'player', 'content': 'He had the Vane brand on his arm, two fingers missing, and the letter named Orrin Vane.'},
            ],
            facts=[
                {
                    'id': 'corpse_unknown',
                    'text': 'The party found an unidentified corpse carrying a half-burned letter that mentioned a thorn and a coach.',
                    'certainty': 'confirmed',
                    'visibility': 'party_known',
                },
            ],
            threads=['Identify the dead courier and learn who wanted the letter.'],
        ),
    },
    {
        'name': 'established_lead_contradiction',
        'expected_safe': False,
        'candidate': 'The tower was never important. The grove is the true heart of the whole operation.',
        'hot_context': scenario_hot_context(
            running_summary='The party learned that the Spike tower is Agent Mercer\'s base and is deciding whether to head there next.',
            current_scene={
                'location_name': 'Ashglass road',
                'immediate_tension': 'The party is debating whether to pursue the tower lead or the grove lead first.',
            },
            recent_messages=[
                {'role': 'dm', 'content': 'The Spike tower remains the clearest lead on Mercer.'},
                {'role': 'player', 'content': 'Could the grove matter more than the tower?'},
            ],
            facts=[
                {
                    'id': 'tower_base',
                    'text': 'The Spike tower is Agent Mercer\'s base of operations.',
                    'certainty': 'confirmed',
                    'visibility': 'party_known',
                },
            ],
            threads=['Decide whether to strike the tower or investigate the grove first.'],
        ),
    },
]


def main():
    results = []
    failures = []
    for scenario in SCENARIOS:
        result = check_session_canon_discipline_with_llm(
            scenario['candidate'],
            scenario['hot_context'],
        )
        actual_safe = bool(result.get('safe'))
        results.append({
            'name': scenario['name'],
            'expected_safe': scenario['expected_safe'],
            'actual_safe': actual_safe,
            'result': result,
        })
        if actual_safe != scenario['expected_safe']:
            failures.append(scenario['name'])

    print(json.dumps({'results': results, 'failures': failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
