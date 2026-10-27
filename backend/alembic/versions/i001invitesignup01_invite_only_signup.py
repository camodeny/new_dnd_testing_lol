"""invite-only sign-up gate (Supabase before-user-created hook).

Revision ID: i001invitesignup01
Revises: u259micros01

Unfunded campaigns have no AI spend cap until the free tier lands (#489),
so open sign-up would let anyone play on the operator's provider bill.
Supabase Auth calls ``public.hook_before_user_created`` before inserting
any new user; it allows the sign-up only when the new user holds a usable
campaign invite:

- ``user_metadata.invite_code`` (sent by the sign-up form from the
  ``/invite/:code`` link) matches an invite, or
- the sign-up email matches an invite's ``intended_email``.

"Usable" mirrors ``app.campaigns.invites.invite_usability`` plus the
deleted-campaign check in ``accept_invite_code``: status ``active``, not
expired, campaign not soft-deleted. Everything else is rejected with 403.
Admin-API user creation (the local test-player seed) does not run the hook.

The function is SECURITY DEFINER so ``supabase_auth_admin`` gets a yes/no
decision without read access to invite tables, and it is not executable by
``anon``/``authenticated``: through the Data API it would otherwise be an
oracle for invite codes and invited emails. The role grants only apply where
the Supabase roles exist (not on the disposable CI database).

Enabling the hook is Auth config, not schema: ``supabase/config.toml``
locally; the hosted project's Auth Hooks settings in production.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "i001invitesignup01"
down_revision: Union[str, Sequence[str], None] = "u259micros01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BEFORE_USER_CREATED_HOOK_SQL = """
CREATE OR REPLACE FUNCTION public.hook_before_user_created(event jsonb)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  signup_email text := lower(btrim(coalesce(event->'user'->>'email', '')));
  invite_code text := upper(btrim(coalesce(event->'user'->'user_metadata'->>'invite_code', '')));
BEGIN
  IF EXISTS (
    SELECT 1
    FROM public.campaign_invites i
    JOIN public.campaigns c ON c.id = i.campaign_id
    WHERE lower(i.status) = 'active'
      AND (i.expires_at IS NULL OR i.expires_at > now())
      AND NOT c.is_deleted
      AND (
        (invite_code <> '' AND i.code = invite_code)
        OR (signup_email <> '' AND lower(i.intended_email) = signup_email)
      )
  ) THEN
    RETURN '{}'::jsonb;
  END IF;
  RETURN jsonb_build_object(
    'error', jsonb_build_object(
      'http_code', 403,
      'message', 'Fireside is invite-only for now. Open your invite link to create an account.'
    )
  );
END
$$;
"""

HOOK_GRANTS_SQL = """
REVOKE ALL ON FUNCTION public.hook_before_user_created(jsonb) FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON FUNCTION public.hook_before_user_created(jsonb) FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON FUNCTION public.hook_before_user_created(jsonb) FROM authenticated;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'supabase_auth_admin') THEN
    GRANT EXECUTE ON FUNCTION public.hook_before_user_created(jsonb) TO supabase_auth_admin;
  END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(BEFORE_USER_CREATED_HOOK_SQL)
    op.execute(HOOK_GRANTS_SQL)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.hook_before_user_created(jsonb)")
