#!/usr/bin/env python3
from pathlib import Path


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{label}: expected one match, found {count}')
    return text.replace(old, new, 1)


test_path = Path('server/tests/test_sqlite_hardening.py')
test_text = test_path.read_text(encoding='utf-8')
test_text = replace_once(
    test_text,
    'import unittest\nfrom datetime import timedelta\n',
    'import unittest\nfrom datetime import timedelta\nfrom pathlib import Path\n',
    'Path test import',
)
test_path.write_text(test_text, encoding='utf-8')

route_path = Path('server/routes/automation.py')
route_text = route_path.read_text(encoding='utf-8')
route_text = replace_once(
    route_text,
    '''        heartbeat_run(
            run,
            worker_id=worker_id,
            lease_token=data.get('lease_token'),
            lease_seconds=lease_seconds,
        )
''',
    '''        run = heartbeat_run(
            run,
            worker_id=worker_id,
            lease_token=data.get('lease_token'),
            lease_seconds=lease_seconds,
        )
''',
    'heartbeat route return value',
)
route_path.write_text(route_text, encoding='utf-8')

print('Applied PR 44 follow-up fixes.')
