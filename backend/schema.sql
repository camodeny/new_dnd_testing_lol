-- Run this in Supabase Dashboard > SQL Editor (once).
-- It creates the thin profiles mirror for Supabase Auth (auth.users).
-- RLS is left off for now; backend uses service role via DATABASE_URL (bypasses RLS).
-- If you later enable RLS, add policies for authenticated users.

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  username text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Keep updated_at fresh
create or replace function public.handle_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists profiles_updated_at on public.profiles;
create trigger profiles_updated_at
  before update on public.profiles
  for each row execute function public.handle_updated_at();

-- Useful index
create index if not exists profiles_email_idx on public.profiles (email);
create index if not exists profiles_username_idx on public.profiles (username);
