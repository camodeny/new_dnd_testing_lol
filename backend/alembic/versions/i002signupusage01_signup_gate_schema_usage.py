"""let Supabase Auth reach the invite-only sign-up gate.

Revision ID: i002signupusage01
Revises: i001invitesignup01

Calling ``public.hook_before_user_created`` needs USAGE on schema ``public``
as well as EXECUTE on the function. Supabase's ``public`` schema grants
``supabase_auth_admin`` no USAGE, so with the hook enabled every sign-up
failed with "Error running hook URI" (plain Postgres grants USAGE to PUBLIC,
which hid this outside Supabase). USAGE only allows name lookup: the auth
role still has no privileges on any table and only gets the gate's decision.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "i002signupusage01"
down_revision: Union[str, Sequence[str], None] = "i001invitesignup01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _for_auth_admin(statement: str) -> str:
    return f"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'supabase_auth_admin') THEN
    {statement};
  END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(_for_auth_admin("GRANT USAGE ON SCHEMA public TO supabase_auth_admin"))


def downgrade() -> None:
    op.execute(_for_auth_admin("REVOKE USAGE ON SCHEMA public FROM supabase_auth_admin"))
