"""Vercel deployment-config guard: cron cadence must fit the Hobby plan.

The repo deploys on Vercel Hobby, which rejects sub-daily cron expressions
(see PR #363: `*/5 * * * *` failed deployment). Every cron in vercel.json
must run at most once per day — minute and hour fields must be numeric
literals so a plan-incompatible cadence fails in CI, not in deployment.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
VERCEL_JSON = REPO_ROOT / "vercel.json"

_NUMERIC = re.compile(r"^\d+$")


def _crons():
    return json.loads(VERCEL_JSON.read_text(encoding="utf-8")).get("crons", [])


def test_vercel_json_is_valid_with_crons():
    crons = _crons()
    assert crons, "vercel.json must define crons"
    for cron in crons:
        assert cron.get("path", "").startswith("/api/cron/"), f"unexpected cron {cron}"
        assert cron.get("schedule", "").strip(), f"cron missing schedule: {cron}"


def test_vercel_crons_fit_hobby_daily_limit():
    """Minute/hour must be numeric literals (at most once per day)."""
    for cron in _crons():
        minute, hour, *_ = cron["schedule"].split()
        assert _NUMERIC.match(minute), f"{cron['path']} minute field {minute!r} runs sub-daily (Hobby rejects it)"
        assert _NUMERIC.match(hour), f"{cron['path']} hour field {hour!r} runs sub-daily (Hobby rejects it)"


def test_post_turn_cron_scheduled():
    paths = [c["path"] for c in _crons()]
    assert "/api/cron/post-turn" in paths
