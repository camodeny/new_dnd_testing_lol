#!/usr/bin/env python3
"""Evaluate a single-DM canonical-private-fact organization against run 53.

The DM receives the full secret. Unlike the production trace, it appears once
as a canonical private fact with explicit disclosure state and public clue
projections; it is not duplicated into public facts, scene prose, threads, or
retrieval text. This is a test fixture, not production routing.
"""
import json
import statistics
import time

from dotenv import load_dotenv

load_dotenv()

from llm_providers import ProviderRequest, execute_chat, provider_registry
from openrouter import SYSTEM_PROMPT


TRIALS = 100
MODEL = 'deepseek-v4-flash'
PROVIDER = 'opencode_go'

PUBLIC_TRANSCRIPT = [
    {'role': 'assistant', 'content': (
        'The late-afternoon mist rolls over Glassmere Ferry Landing. Water is rising over the pilings as the '
        'tidelocks remain stalled. Bram Harlow, the lockkeeper, insists his rounds were clean while a finely '
        'dressed nobleman watches from a balcony.\n\nWhat do you do?'
    )},
    {'role': 'user', 'content': (
        'Before we rush off, Mr. Harlow—you said you did everything right, but you look like you are expecting '
        'the locks to fail. What did you actually find on your rounds last night?'
    )},
    {'role': 'assistant', 'content': (
        'Bram gives a rehearsed denial. His eyes flick toward the lockhouse door and then toward the balcony. '
        'He seems frightened and evasive. Roll Persuasion to press him or Insight to read his tells.'
    )},
    {'role': 'user', 'content': (
        'I watch Bram’s eyes as he talks—the flick to the lockhouse, then to the nobleman. '
        'Let’s see what he is really afraid of. [Roll: Insight check] total: 5 | rolls: 5 | mod: 0 | sides: 20'
    )},
]

CANONICAL_FACTS = [
    {
        'id': 'fact_bram_forged_ledger',
        'truth': 'Bram Harlow forged the mirror-check entry in the lockhouse ledger to conceal a visitor.',
        'visibility': 'dm_private',
        'disclosure_state': 'unrevealed',
        'reveal_requirements': ['Bram confesses after an earned social breakthrough', 'party examines the ledger'],
        'permitted_public_projections': [
            {'id': 'bram_evasive', 'text': 'Bram is evasive and afraid of blame.', 'available_now': True},
            {'id': 'lockhouse_suspicious', 'text': 'Bram’s attention repeatedly returns to the lockhouse.', 'available_now': True},
        ],
    },
]


def prompt_messages():
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'system', 'content': (
            'Canonical campaign state for this DM turn. The DM receives the complete truth and should use it for '
            'NPC motivation, pacing, and continuity. Visibility is a property of each canonical fact, not a cue to '
            'copy private prose into narration. For an unrevealed fact, final visible content may express only a '
            'permitted_public_projection whose available_now value is true. It may not state a truth field or a '
            'reveal requirement, including as a question, hypothesis, contrast, metaphor, or example. The player '
            'rolled a failed Insight check, so no new projection becomes available.\n'
            + json.dumps({'canonical_facts': CANONICAL_FACTS}, ensure_ascii=False)
        )},
        *PUBLIC_TRANSCRIPT,
    ]


def main():
    adapter = provider_registry.get(PROVIDER)
    rows = []
    for index in range(1, TRIALS + 1):
        started = time.monotonic()
        try:
            response = execute_chat(adapter, ProviderRequest(
                messages=prompt_messages(),
                model=MODEL,
                timeout_seconds=180,
                max_attempts=1,
            ))
            text = (response.content or '').lower()
            # Metrics are intentionally narrow and only inspect the known regression phrase.
            rows.append({'ok': True, 'leak': 'forged ledger' in text, 'latency_ms': round((time.monotonic() - started) * 1000)})
        except Exception as error:
            rows.append({'ok': False, 'error': type(error).__name__})
        if index % 10 == 0:
            completed = [row for row in rows if row['ok']]
            print(json.dumps({
                'progress': index,
                'successful': len(completed),
                'known_phrase_leaks': sum(row['leak'] for row in completed),
                'errors': index - len(completed),
                'mean_latency_ms': round(statistics.mean(row['latency_ms'] for row in completed), 1) if completed else None,
            }), flush=True)
    completed = [row for row in rows if row['ok']]
    print(json.dumps({
        'final': True,
        'trials': TRIALS,
        'successful': len(completed),
        'known_phrase_leaks': sum(row['leak'] for row in completed),
        'errors': TRIALS - len(completed),
        'mean_latency_ms': round(statistics.mean(row['latency_ms'] for row in completed), 1) if completed else None,
        'median_latency_ms': round(statistics.median(row['latency_ms'] for row in completed), 1) if completed else None,
    }), flush=True)


if __name__ == '__main__':
    main()
