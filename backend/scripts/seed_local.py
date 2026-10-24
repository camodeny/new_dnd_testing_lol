"""Seed the local Supabase stack for gameplay testing.

Run by ``scripts/local-supabase.sh up`` after migrations; see docs/local-dev.md.
Schedules the same pg_cron sweeps production runs
(``scripts/supabase/schedule_*.sql``), aimed at the backend on this machine,
and creates the test players the login page offers on the local stack
(``frontend/lib/localSupabase.ts``). Refuses to touch anything that is not
127.0.0.1/localhost.

    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... CRON_SECRET=... \\
    LOCAL_BACKEND_ORIGIN=http://host.docker.internal:8000 \\
    python -m scripts.seed_local
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import uuid
from urllib.parse import urlparse

import httpx
from sqlalchemy import text

from database import DATABASE_URL, SessionLocal
from app.auth.service import upsert_profile

LOCAL_HOSTS = {"127.0.0.1", "localhost"}

# Keep in sync with LOCAL_TEST_PLAYERS in frontend/lib/localSupabase.ts.
TEST_PLAYER_COUNT = 4
TEST_PLAYER_PASSWORD = "password"

# (job, schedule, endpoint, HTTP timeout ms), as in scripts/supabase/schedule_*.sql.
CRON_SWEEPS = [
    ("dnd-dm-execute", "* * * * *", "/api/cron/dm-execute", 300000),
    ("dnd-post-turn", "* * * * *", "/api/cron/post-turn", 120000),
    ("dnd-adventure-closing", "*/5 * * * *", "/api/cron/adventure-closing", 120000),
]


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"seed_local: {name} is not set")
    return value


def _require_local(name: str, url: str) -> None:
    if urlparse(url).hostname not in LOCAL_HOSTS:
        sys.exit(f"seed_local: refusing to run, {name} is not a local URL")


def schedule_sweeps(db, backend_origin: str, cron_secret: str) -> None:
    db.execute(text("CREATE EXTENSION IF NOT EXISTS pg_cron"))
    db.execute(text("CREATE EXTENSION IF NOT EXISTS pg_net"))
    headers = json.dumps({"Content-Type": "application/json", "Authorization": f"Bearer {cron_secret}"})
    for job, schedule, endpoint, timeout_ms in CRON_SWEEPS:
        # cron.schedule replaces a job of the same name, so re-runs converge.
        db.execute(
            text(
                "SELECT cron.schedule(:job, :schedule, format("
                "'SELECT net.http_post(url := %L, headers := %L::jsonb,"
                " body := ''{}''::jsonb, timeout_milliseconds := %s)',"
                " :url, :headers, :timeout_ms))"
            ),
            {
                "job": job,
                "schedule": schedule,
                "url": backend_origin.rstrip("/") + endpoint,
                "headers": headers,
                "timeout_ms": timeout_ms,
            },
        )
    db.commit()


def seed_test_players(db, supabase_url: str, service_role_key: str) -> list[str]:
    admin = httpx.Client(
        base_url=f"{supabase_url.rstrip('/')}/auth/v1/admin",
        headers={"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"},
        timeout=10.0,
    )
    with admin:
        listing = admin.get("/users", params={"per_page": 1000})
        listing.raise_for_status()
        existing = {user["email"]: user["id"] for user in listing.json()["users"]}
        emails = []
        for n in range(1, TEST_PLAYER_COUNT + 1):
            email, username = f"player{n}@local.test", f"player{n}"
            body = {
                "email": email,
                "password": TEST_PLAYER_PASSWORD,
                "email_confirm": True,
                "user_metadata": {"username": username},
            }
            # Re-runs reset the password so the login page's one-click sign-in keeps working.
            if email in existing:
                resp = admin.put(f"/users/{existing[email]}", json=body)
            else:
                resp = admin.post("/users", json=body)
            resp.raise_for_status()
            upsert_profile(db, uuid.UUID(resp.json()["id"]), email=email, username=username)
            emails.append(email)
    return emails


def main() -> None:
    supabase_url = _require_env("SUPABASE_URL")
    service_role_key = _require_env("SUPABASE_SERVICE_ROLE_KEY")
    cron_secret = _require_env("CRON_SECRET")
    backend_origin = _require_env("LOCAL_BACKEND_ORIGIN")
    _require_local("SUPABASE_URL", supabase_url)
    _require_local("the database URL", DATABASE_URL)

    db = SessionLocal()
    try:
        schedule_sweeps(db, backend_origin, cron_secret)
        print(f"cron sweeps -> {backend_origin}: {', '.join(job for job, *_ in CRON_SWEEPS)}")
        emails = seed_test_players(db, supabase_url, service_role_key)
        print(f"test players (password {TEST_PLAYER_PASSWORD!r}): {', '.join(emails)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
