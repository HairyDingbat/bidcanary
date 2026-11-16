-- Bid Canary database schema for Supabase (Postgres).
-- Paste into Supabase > SQL Editor > New query, and run once.
-- Security model: the public website (anon key) can ONLY add a free signup and unsubscribe by token.
-- Everything else is readable/writable only by the engine using the service key (GitHub secret).

create extension if not exists pgcrypto;

create table if not exists subscribers (
  id uuid primary key default gen_random_uuid(),
  email text not null unique check (email ~* '^[^@\s]+@[^@\s]+\.[^@\s]+$' and length(email) <= 254),
  pack text not null check (pack ~ '^[a-z]{2,20}$'),
  keywords text check (length(keywords) <= 200),
  regions text not null default '' check (regions ~ '^(UK[C-N](,UK[C-N])*)?$'),
  plan text not null default 'free' check (plan in ('free', 'starter', 'pro')),
  status text not null default 'pending' check (status in ('pending', 'active', 'unsubscribed')),
  unsub_token uuid not null default gen_random_uuid(),
  consent_text text check (length(consent_text) <= 500),
  confirm_sent_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists notices (
  ocid text primary key,
  title text, description text, buyer text, value numeric,
  deadline timestamptz, cpvs jsonb, regions jsonb, url text, summary text,
  seen_at timestamptz default now()
);

create table if not exists sent (
  subscriber_id uuid references subscribers(id) on delete cascade,
  ocid text,
  sent_at timestamptz default now(),
  primary key (subscriber_id, ocid)
);

create table if not exists state (k text primary key, v text);

alter table subscribers enable row level security;
alter table notices enable row level security;
alter table sent enable row level security;
alter table state enable row level security;

-- Website signups: insert only, only these columns, only as free + active. No reading back.
revoke all on subscribers from anon, authenticated;
grant insert (email, pack, regions, keywords, consent_text) on subscribers to anon;
drop policy if exists "public free signup" on subscribers;
create policy "public free signup" on subscribers for insert to anon
  with check (plan = 'free' and status = 'pending');

-- Unsubscribe link: works with the secret token in the email, reveals nothing.
create or replace function confirm_subscription(token uuid) returns boolean
language sql security definer set search_path = public as $$
  with u as (update subscribers set status = 'active' where unsub_token = token and status = 'pending' returning 1)
  select exists (select 1 from u);
$$;
revoke all on function confirm_subscription(uuid) from public;
grant execute on function confirm_subscription(uuid) to anon;

create or replace function unsubscribe(token uuid) returns boolean
language sql security definer set search_path = public as $$
  with u as (update subscribers set status = 'unsubscribed' where unsub_token = token returning 1)
  select exists (select 1 from u);
$$;
revoke all on function unsubscribe(uuid) from public;
grant execute on function unsubscribe(uuid) to anon;

-- Data retention helper: remove notices closed more than 18 months ago (run monthly by the engine or manually).
create or replace function prune_old_notices() returns void language sql as $$
  delete from notices where deadline < now() - interval '18 months';
$$;
revoke all on function prune_old_notices() from public, anon, authenticated;
