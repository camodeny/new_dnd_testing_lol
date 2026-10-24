// Affordances for `next dev` against the local Supabase stack
// (scripts/local-supabase.sh, docs/local-dev.md). Off in production builds and
// against any hosted project.

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL ?? ''

export const isLocalSupabase =
  process.env.NODE_ENV !== 'production' && /^http:\/\/(127\.0\.0\.1|localhost):\d+\/?$/.test(supabaseUrl)

// Seeded by backend/scripts/seed_local.py — keep the two in sync.
export const LOCAL_TEST_PLAYERS = [1, 2, 3, 4].map((n) => ({
  label: `Player ${n}`,
  email: `player${n}@local.test`,
}))
export const LOCAL_TEST_PLAYER_PASSWORD = 'password'
