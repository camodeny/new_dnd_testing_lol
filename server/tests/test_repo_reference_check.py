import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

REPO_ROOT = Path(__file__).resolve().parents[2]
# Built from parts so this test file does not contain the literal forbidden strings.
FORBIDDEN_PATTERNS = (
    'codex' + '_issue' + '_launcher',
    'Codex' + 'IssueLauncher',
)
SKIPPED_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', 'dist', 'build'}


class RepoReferenceCheckTest(unittest.TestCase):
    def test_obsolete_launcher_references_absent(self):
        violations = []
        for path in REPO_ROOT.rglob('*'):
            if path.is_dir():
                continue
            relative = path.relative_to(REPO_ROOT)
            if any(part in SKIPPED_DIRS for part in relative.parts):
                continue
            try:
                text = path.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            if any(pattern in text for pattern in FORBIDDEN_PATTERNS):
                violations.append(str(relative))

        self.assertEqual(
            violations,
            [],
            'Obsolete Codex issue launcher references must be removed. Found in: {0}'.format(
                ', '.join(violations)
            ),
        )
