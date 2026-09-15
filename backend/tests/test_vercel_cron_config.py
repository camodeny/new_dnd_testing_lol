"""Deployment scheduling guards.

- Vercel deploys on the Hobby plan, which rejects sub-daily cron expressions
  (see PR #363). Every cron in vercel.json must run at most once per day.
- Fast production scheduling lives in Supabase pg_cron + pg_net SQL files
  (see backend/scripts/supabase/README.md). The post-turn schedule must hit
  the cron endpoint every minute with Vault-backed auth, re-runnable without
  duplicating the named job.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
VERCEL_JSON = REPO_ROOT / "vercel.json"
SUPABASE_DIR = Path(__file__).parent.parent / "scripts" / "supabase"

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


def test_post_turn_supabase_schedule_runs_minutely():
    """Post-turn fast execution: pg_cron every minute on the cron endpoint."""
    sql = (SUPABASE_DIR / "schedule_post_turn.sql").read_text(encoding="utf-8")
    assert "cron.schedule(" in sql
    assert "'dnd-post-turn', '* * * * *'" in sql
    assert "/api/cron/post-turn" in sql
    assert "net.http_post(" in sql
    assert "post_turn_base_url" in sql
    assert "post_turn_cron_secret" in sql
    assert "Authorization" in sql
    # Re-running must replace the named job, never duplicate schedules.
    assert "Re-running replaces the named job" in sql
    assert "cron.unschedule('dnd-post-turn')" in sql
