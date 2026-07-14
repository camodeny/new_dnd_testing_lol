#!/usr/bin/env python3
from pathlib import Path
import re


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{label}: expected one match, found {count}')
    return text.replace(old, new, 1)


DM_RESPONSE_STATE = '''"""Shared phase-aware DM response polling state machine."""

import time


def dm_turn_has_visible_output(status_dict):
    if not isinstance(status_dict, dict):
        return False
    status = str(status_dict.get('status') or '').strip().lower()
    if status in {'speak', 'silent', 'empty', 'error'}:
        return True
    return status_dict.get('dm_message_id') is not None


def dm_turn_post_turn_resolved(status_dict):
    if not isinstance(status_dict, dict):
        return False
    status = str(status_dict.get('status') or '').strip().lower()
    if status in {'silent', 'empty', 'error'}:
        return True

    post_turn = str(status_dict.get('post_turn_status') or '').strip().lower()
    if post_turn in {'complete', 'error'}:
        return True

    if 'post_turn_complete' in status_dict:
        return bool(status_dict.get('post_turn_complete'))

    # Backward compatibility for servers that only report visible output state.
    return status == 'speak'


def dm_turn_fully_resolved(status_dict):
    if not isinstance(status_dict, dict):
        return False
    status = str(status_dict.get('status') or '').strip().lower()
    if not status or status == 'pending':
        return False
    return dm_turn_post_turn_resolved(status_dict)


def classify_timeout(status_dict, phase):
    if phase == 'visible':
        return 'dm_visible_response_timeout'
    if phase == 'post_turn':
        if str(status_dict.get('post_turn_status') or '').strip().lower() == 'error':
            return 'dm_post_turn_error'
        return 'dm_post_turn_timeout'
    return 'dm_response_timeout'


def build_timeout_evidence(status_dict, phase):
    del phase
    return {
        'player_message_id': status_dict.get('player_message_id'),
        'dm_message_id': status_dict.get('dm_message_id'),
        'status': status_dict.get('status'),
        'post_turn_status': status_dict.get('post_turn_status'),
        'memory_status': status_dict.get('memory_status'),
        'clock_status': status_dict.get('clock_status'),
        'started_at': status_dict.get('started_at'),
        'visible_completed_at': status_dict.get('visible_completed_at'),
        'finished_at': status_dict.get('finished_at'),
        'generation_duration_ms': status_dict.get('generation_duration_ms'),
        'full_duration_ms': status_dict.get('full_duration_ms'),
    }


def resolve_dm_response_timeouts(args):
    visible = getattr(args, 'dm_visible_response_timeout', None)
    post_turn = getattr(args, 'dm_post_turn_timeout', None)
    legacy = getattr(args, 'dm_response_timeout', None)

    if visible is not None and post_turn is not None:
        return float(visible), float(post_turn)
    if visible is not None:
        return float(visible), 180.0
    if post_turn is not None:
        return 300.0, float(post_turn)
    if legacy is not None:
        return float(legacy), 180.0
    return 300.0, 180.0


def _fetch_status(fetch_status_fn, last_status, phase, transient_error_types, on_poll_error_fn):
    try:
        return fetch_status_fn()
    except transient_error_types as exc:
        if on_poll_error_fn is not None:
            on_poll_error_fn(exc, phase)
        return last_status


def wait_for_dm_response(
    fetch_status_fn,
    maybe_heartbeat_fn,
    visible_timeout,
    post_turn_timeout,
    poll_interval,
    *,
    transient_error_types=(Exception,),
    on_poll_error_fn=None,
):
    """Poll through visible-output and post-turn phases with separate deadlines."""
    monotonic = time.monotonic
    sleep_interval = max(0.05, float(poll_interval))

    visible_deadline = monotonic() + max(0.0, float(visible_timeout))
    last_status = {'status': 'pending'}

    while True:
        maybe_heartbeat_fn()
        last_status = _fetch_status(
            fetch_status_fn,
            last_status,
            'visible',
            transient_error_types,
            on_poll_error_fn,
        )
        if dm_turn_has_visible_output(last_status):
            break
        if monotonic() >= visible_deadline:
            last_status = _fetch_status(
                fetch_status_fn,
                last_status,
                'visible_final',
                transient_error_types,
                on_poll_error_fn,
            )
            if dm_turn_has_visible_output(last_status):
                break
            return last_status, True, 'visible'
        remaining = visible_deadline - monotonic()
        time.sleep(min(sleep_interval, max(0.05, remaining)))

    post_turn_deadline = monotonic() + max(0.0, float(post_turn_timeout))
    while True:
        maybe_heartbeat_fn()
        last_status = _fetch_status(
            fetch_status_fn,
            last_status,
            'post_turn',
            transient_error_types,
            on_poll_error_fn,
        )
        if dm_turn_post_turn_resolved(last_status):
            return last_status, False, None
        if monotonic() >= post_turn_deadline:
            last_status = _fetch_status(
                fetch_status_fn,
                last_status,
                'post_turn_final',
                transient_error_types,
                on_poll_error_fn,
            )
            if dm_turn_post_turn_resolved(last_status):
                return last_status, False, None
            return last_status, True, 'post_turn'
        remaining = post_turn_deadline - monotonic()
        time.sleep(min(sleep_interval, max(0.05, remaining)))
'''

Path('automation/dm_response_state.py').write_text(DM_RESPONSE_STATE, encoding='utf-8')

# Pass the late-completion reconciliation window through automationctl.
ctl_path = Path('automation/automationctl.py')
ctl_text = ctl_path.read_text(encoding='utf-8')
ctl_text = replace_once(
    ctl_text,
    "        ('dm_post_turn_timeout', '--dm-post-turn-timeout'),\n",
    "        ('dm_post_turn_timeout', '--dm-post-turn-timeout'),\n        ('dm_late_completion_reconciliation_seconds', '--dm-late-completion-reconciliation-seconds'),\n",
    'automationctl passthrough',
)
ctl_text = replace_once(
    ctl_text,
    "    worker_start.add_argument('--dm-post-turn-timeout', type=float)\n",
    "    worker_start.add_argument('--dm-post-turn-timeout', type=float)\n    worker_start.add_argument('--dm-late-completion-reconciliation-seconds', type=float)\n",
    'automationctl parser',
)
ctl_path.write_text(ctl_text, encoding='utf-8')

worker_path = Path('automation/run_automation_worker.py')
worker_text = worker_path.read_text(encoding='utf-8')
worker_text = replace_once(
    worker_text,
    "    parser.add_argument('--dm-post-turn-timeout', type=float, default=float(_post_turn_env) if _post_turn_env else None)\n",
    "    parser.add_argument('--dm-post-turn-timeout', type=float, default=float(_post_turn_env) if _post_turn_env else None)\n    parser.add_argument(\n        '--dm-late-completion-reconciliation-seconds',\n        type=float,\n        default=float(os.environ.get('DND_DM_LATE_COMPLETION_RECONCILIATION_SECONDS', '30')),\n    )\n",
    'worker reconciliation parser',
)

# Replace the one-shot pre-failure probe with bounded post-failure reconciliation.
late_pattern = re.compile(
    r"def _check_late_completion\(.*?\n\n\ndef wait_for_dm_response",
    re.DOTALL,
)
late_replacement = '''def _reconcile_late_completion(
    args,
    manifest,
    player_message_id,
    run_id,
    lease_token,
    timeout_phase,
    timeout_error,
    failure_timestamp,
):
    if timeout_phase != 'post_turn':
        return

    reconciliation_seconds = max(
        0.0,
        float(getattr(args, 'dm_late_completion_reconciliation_seconds', 30.0)),
    )
    deadline = time.monotonic() + reconciliation_seconds
    last_status = None

    while True:
        try:
            last_status = autonomous.fetch_dm_turn_status(manifest, player_message_id)
        except ApiError:
            pass
        else:
            post_turn_status = str(last_status.get('post_turn_status') or '').strip().lower()
            if post_turn_status == 'complete':
                append_event(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    args.worker_id,
                    lease_token,
                    'dm_turn_late_completion',
                    {
                        'automation_run_id': run_id,
                        'player_message_id': player_message_id,
                        'dm_message_id': last_status.get('dm_message_id'),
                        'failure_timestamp': failure_timestamp,
                        'completion_timestamp': last_status.get('finished_at') or utc_now(),
                        'original_failure_classification': timeout_error,
                        'final_status': last_status.get('status'),
                        'final_post_turn_status': post_turn_status,
                        'final_memory_status': last_status.get('memory_status'),
                        'final_clock_status': last_status.get('clock_status'),
                    },
                    dedupe_key=f'dm_turn_late_completion:{run_id}:{player_message_id}',
                )
                return
            if post_turn_status == 'error' or last_status.get('status') == 'error':
                return

        if time.monotonic() >= deadline:
            return
        remaining = deadline - time.monotonic()
        time.sleep(min(args.poll_interval, max(0.05, remaining)))


def wait_for_dm_response'''
worker_text, count = late_pattern.subn(late_replacement, worker_text, count=1)
if count != 1:
    raise RuntimeError(f'late completion helper replacement: expected one match, found {count}')

# Use only ApiError as a transient polling failure; programming errors must propagate.
wait_pattern = re.compile(
    r"def wait_for_dm_response\(args, manifest, player_message_id, maybe_heartbeat_fn\):\n.*?\n\n\ndef pause_for_audit_if_needed",
    re.DOTALL,
)
wait_replacement = '''def wait_for_dm_response(args, manifest, player_message_id, maybe_heartbeat_fn):
    visible_timeout, post_turn_timeout = dm_response_state.resolve_dm_response_timeouts(args)

    def fetch_status():
        return autonomous.fetch_dm_turn_status(manifest, player_message_id)

    return dm_response_state.wait_for_dm_response(
        fetch_status,
        maybe_heartbeat_fn,
        visible_timeout,
        post_turn_timeout,
        args.poll_interval,
        transient_error_types=(ApiError,),
    )


def pause_for_audit_if_needed'''
worker_text, count = wait_pattern.subn(wait_replacement, worker_text, count=1)
if count != 1:
    raise RuntimeError(f'worker wait replacement: expected one match, found {count}')

# Ensure resolved errors create after_dm audit evidence before the run is failed.
error_pattern = re.compile(
    r"(?P<indent> +)if dm_turn\.get\('status'\) == 'error' or dm_turn\.get\('post_turn_status'\) == 'error':\n"
    r".*?"
    r"(?P=indent)    return True\n"
    r"(?P=indent)if dm_timed_out:",
    re.DOTALL,
)

def error_replacement(match):
    i = match.group('indent')
    j = i + '    '
    k = j + '    '
    return f"""{i}if dm_turn.get('status') == 'error' or dm_turn.get('post_turn_status') == 'error':
{j}should_stop, lease_token = pause_for_audit_if_needed(
{k}args,
{k}claim_payload,
{k}run_id,
{k}lease_token,
{k}'after_dm',
{k}maybe_heartbeat,
{k}payload={{
{k}    'dm_turn': dm_turn,
{k}    'posted_message_id': posted_message_id,
{k}    'turns_completed': turns_completed,
{k}}},
{k}summary='Paused after resolved DM error.',
{k}player_message_id=posted_message_id,
{k}dm_message_id=dm_turn.get('dm_message_id'),
{k}dedupe_key=f'audit_pause:after_dm:{{logical_key}}:{{posted_message_id}}',
{j})
{j}if should_stop:
{k}complete_run(
{k}    args.api_base,
{k}    args.owner_api_key,
{k}    run_id,
{k}    args.worker_id,
{k}    lease_token,
{k}    status='stopped',
{k}    dedupe_key=f'run_completed:{{run_id}}:audit-stop',
{k})
{k}return True

{j}failure_payload = {{
{k}'player_message_id': posted_message_id,
{k}'dm_message_id': dm_turn.get('dm_message_id'),
{k}'status': dm_turn.get('status'),
{k}'post_turn_status': dm_turn.get('post_turn_status'),
{k}'memory_status': dm_turn.get('memory_status', 'skipped'),
{k}'clock_status': dm_turn.get('clock_status', 'skipped'),
{k}'turn_error': dm_turn.get('turn_error') or dm_turn.get('error_text') or dm_turn.get('post_turn_error'),
{k}'phase': 'after_dm',
{k}'retry_count': 0,
{k}'skipped_downstream_expectations': [
{k}    'memory_validation',
{k}    'clock_validation',
{k}],
{j}}}
{j}append_event(
{k}args.api_base,
{k}args.owner_api_key,
{k}run_id,
{k}args.worker_id,
{k}lease_token,
{k}'dm_turn_failed',
{k}failure_payload,
{k}dedupe_key=f'dm_turn_failed:{{logical_key}}:{{posted_message_id}}',
{j})
{j}error_classification = (
{k}'dm_post_turn_error'
{k}if str(dm_turn.get('post_turn_status') or '').strip().lower() == 'error'
{k}else (dm_turn.get('turn_error') or dm_turn.get('error_text') or 'dm_turn_failed')
{j})
{j}complete_run(
{k}args.api_base,
{k}args.owner_api_key,
{k}run_id,
{k}args.worker_id,
{k}lease_token,
{k}status='failed',
{k}error_text=error_classification,
{k}dedupe_key=f'run_completed:{{run_id}}:dm-failure:{{posted_message_id}}',
{j})
{j}return True
{i}if dm_timed_out:"""

worker_text, count = error_pattern.subn(error_replacement, worker_text)
if count != 2:
    raise RuntimeError(f'error branch replacement: expected two matches, found {count}')

# Fail first, then perform bounded reconciliation and append immutable late evidence.
timeout_pattern = re.compile(
    r"(?P<indent> +)if dm_timed_out:\n"
    r".*?"
    r"(?P=indent)    return True\n"
    r"(?P=indent)last_dm_turn =",
    re.DOTALL,
)

def timeout_replacement(match):
    i = match.group('indent')
    j = i + '    '
    k = j + '    '
    return f"""{i}if dm_timed_out:
{j}timeout_error = dm_response_state.classify_timeout(dm_turn, timeout_phase)
{j}timeout_evidence = dm_response_state.build_timeout_evidence(dm_turn, timeout_phase)
{j}timeout_evidence['timeout_phase'] = timeout_phase
{j}append_event(
{k}args.api_base,
{k}args.owner_api_key,
{k}run_id,
{k}args.worker_id,
{k}lease_token,
{k}'dm_turn_timeout',
{k}timeout_evidence,
{k}dedupe_key=f'dm_turn_timeout:{{logical_key}}:{{posted_message_id}}:{{timeout_phase}}',
{j})
{j}failure_timestamp = utc_now()
{j}complete_run(
{k}args.api_base,
{k}args.owner_api_key,
{k}run_id,
{k}args.worker_id,
{k}lease_token,
{k}status='failed',
{k}error_text=timeout_error,
{k}dedupe_key=f'run_completed:{{run_id}}:dm-timeout:{{timeout_phase}}',
{j})
{j}_reconcile_late_completion(
{k}args,
{k}manifest,
{k}posted_message_id,
{k}run_id,
{k}lease_token,
{k}timeout_phase,
{k}timeout_error,
{k}failure_timestamp,
{j})
{j}return True
{i}last_dm_turn ="""

worker_text, count = timeout_pattern.subn(timeout_replacement, worker_text)
if count != 2:
    raise RuntimeError(f'timeout branch replacement: expected two matches, found {count}')
worker_path.write_text(worker_text, encoding='utf-8')

# Restore standalone polling telemetry and restrict swallowed errors to ApiError.
autonomous_path = Path('automation/run_autonomous_llm_campaign.py')
autonomous_text = autonomous_path.read_text(encoding='utf-8')
old_call = '''    last_status, timed_out, timeout_phase = dm_response_state.wait_for_dm_response(
        fetch_status,
        lambda: None,
        visible_timeout,
        post_turn_timeout,
        args.poll_interval,
    )
    return last_status, timed_out, timeout_phase
'''
new_call = '''    def on_poll_error(exc, phase):
        print_event({
            'event': 'dm_turn_status_poll_error',
            'timestamp': utc_now(),
            'player_message_id': player_message_id,
            'phase': phase,
            'error': str(exc),
        })

    return dm_response_state.wait_for_dm_response(
        fetch_status,
        lambda: None,
        visible_timeout,
        post_turn_timeout,
        args.poll_interval,
        transient_error_types=(ApiError,),
        on_poll_error_fn=on_poll_error,
    )
'''
autonomous_text = replace_once(autonomous_text, old_call, new_call, 'standalone polling telemetry')
autonomous_path.write_text(autonomous_text, encoding='utf-8')

# Update state-machine expectations and add focused regression tests.
state_test_path = Path('automation/test_dm_response_state.py')
state_tests = state_test_path.read_text(encoding='utf-8')
state_tests = state_tests.replace(
    "    def test_dm_turn_post_turn_resolved_no_post_turn_info(self):\n        self.assertFalse(dm_response_state.dm_turn_post_turn_resolved({'status': 'speak'}))\n",
    "    def test_dm_turn_post_turn_resolved_no_post_turn_info(self):\n        self.assertTrue(dm_response_state.dm_turn_post_turn_resolved({'status': 'speak'}))\n",
)
insert_marker = "\n\nif __name__ == '__main__':\n"
extra_state_tests = '''

class DmResponseStateReviewRegressionTests(unittest.TestCase):
    def test_generation_error_is_terminal_without_waiting_for_timeout(self):
        status = {'status': 'error', 'turn_error': 'provider failed'}
        result, timed_out, phase = dm_response_state.wait_for_dm_response(
            lambda: status,
            lambda: None,
            300,
            180,
            1,
        )
        self.assertFalse(timed_out)
        self.assertIsNone(phase)
        self.assertEqual(result, status)

    def test_non_transient_programming_error_propagates(self):
        with self.assertRaises(ValueError):
            dm_response_state.wait_for_dm_response(
                lambda: (_ for _ in ()).throw(ValueError('bug')),
                lambda: None,
                300,
                180,
                1,
                transient_error_types=(RuntimeError,),
            )

    def test_transient_error_is_reported_with_phase(self):
        statuses = iter([
            RuntimeError('temporary'),
            {'status': 'speak', 'post_turn_status': 'complete'},
        ])
        reported = []

        def fetch():
            item = next(statuses)
            if isinstance(item, Exception):
                raise item
            return item

        with patch.object(time, 'sleep'):
            result, timed_out, phase = dm_response_state.wait_for_dm_response(
                fetch,
                lambda: None,
                300,
                180,
                1,
                transient_error_types=(RuntimeError,),
                on_poll_error_fn=lambda exc, poll_phase: reported.append((str(exc), poll_phase)),
            )

        self.assertFalse(timed_out)
        self.assertIsNone(phase)
        self.assertEqual(result['post_turn_status'], 'complete')
        self.assertEqual(reported, [('temporary', 'visible')])
'''
if insert_marker not in state_tests:
    raise RuntimeError('state test insertion marker missing')
state_tests = state_tests.replace(insert_marker, extra_state_tests + insert_marker, 1)
state_test_path.write_text(state_tests, encoding='utf-8')

# Add a worker-level test proving reconciliation can observe completion after more than five seconds.
worker_test_path = Path('automation/test_run_automation_worker.py')
worker_tests = worker_test_path.read_text(encoding='utf-8')
extra_worker_tests = '''

class LateCompletionReconciliationTest(unittest.TestCase):
    def test_reconciliation_records_completion_after_multiple_polls(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='worker-1',
            poll_interval=3.0,
            dm_late_completion_reconciliation_seconds=30.0,
        )
        statuses = [
            {'status': 'speak', 'post_turn_status': 'pending', 'dm_message_id': 20},
            {'status': 'speak', 'post_turn_status': 'pending', 'dm_message_id': 20},
            {
                'status': 'speak',
                'post_turn_status': 'complete',
                'dm_message_id': 20,
                'memory_status': 'complete',
                'clock_status': 'complete',
                'finished_at': '2026-07-14T02:26:03Z',
            },
        ]

        with patch.object(worker.autonomous, 'fetch_dm_turn_status', side_effect=statuses) as fetch, \
                patch.object(worker.time, 'monotonic', return_value=0.0), \
                patch.object(worker.time, 'sleep') as sleep, \
                patch.object(worker, 'append_event') as append:
            worker._reconcile_late_completion(
                args,
                {'session': {'id': 4}},
                10,
                5,
                'lease-1',
                'post_turn',
                'dm_post_turn_timeout',
                '2026-07-14T02:25:56Z',
            )

        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        payload = append.call_args[0][6]
        self.assertEqual(append.call_args[0][5], 'dm_turn_late_completion')
        self.assertEqual(payload['failure_timestamp'], '2026-07-14T02:25:56Z')
        self.assertEqual(payload['final_post_turn_status'], 'complete')
'''
if insert_marker not in worker_tests:
    raise RuntimeError('worker test insertion marker missing')
worker_tests = worker_tests.replace(insert_marker, extra_worker_tests + insert_marker, 1)
worker_test_path.write_text(worker_tests, encoding='utf-8')

# Update P0 tests for the new audit-before-failure behavior and classification.
p0_path = Path('server/tests/test_p0_fixes.py')
p0_text = p0_path.read_text(encoding='utf-8')
p0_text = p0_text.replace(
    "             patch.object(worker, 'heartbeat') as mock_heart:\n",
    "             patch.object(worker, 'heartbeat') as mock_heart, \\\n             patch.object(worker, 'pause_for_audit_if_needed', return_value=(False, 'lease-1')) as pause_mock:\n",
    2,
)
p0_text = replace_once(
    p0_text,
    "            error_text='Failed to compile memory patch',\n",
    "            error_text='dm_post_turn_error',\n",
    'post-turn error classification expectation',
)
p0_text = replace_once(
    p0_text,
    "        self.assertIn('dm_response_audit', payload['skipped_downstream_expectations'])\n",
    "        self.assertNotIn('dm_response_audit', payload['skipped_downstream_expectations'])\n        pause_mock.assert_called_once()\n",
    'visible error audit expectation',
)
p0_text = replace_once(
    p0_text,
    "        self.assertEqual(payload['turn_error'], 'Failed to compile memory patch')\n",
    "        self.assertEqual(payload['turn_error'], 'Failed to compile memory patch')\n        pause_mock.assert_called_once()\n",
    'post-turn error audit expectation',
)
p0_path.write_text(p0_text, encoding='utf-8')

print('Applied PR 43 review fixes.')
