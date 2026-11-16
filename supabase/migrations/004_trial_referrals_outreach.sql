-- Migration 004: 14-day trial, annual plan support, founding members, referrals, cold outreach.
-- Run once in Supabase > SQL Editor. Safe to re-run.

-- ---------------------------------------------------------------- trial, founding, referrals
alter table subscribers add column if not exists trial_ends_at timestamptz;
alter table subscribers add column if not exists trial_end_notified_at timestamptz;
alter table subscribers add column if not exists founding boolean not null default false;
alter table subscribers add column if not exists ref_code text unique
  default substr(replace(gen_random_uuid()::text, '-', ''), 1, 8);
update subscribers set ref_code = substr(replace(gen_random_uuid()::text, '-', ''), 1, 8) where ref_code is null;
alter table subscribers add column if not exists referred_by text check (referred_by ~ '^[a-f0-9]{8}$');
alter table subscribers add column if not exists referral_flagged_at timestamptz;

-- The website may also record who referred a sign-up. It still cannot set a plan, a trial or a status.
grant insert (email, pack, regions, keywords, consent_text, referred_by) on subscribers to anon;

-- Confirming starts the 14-day trial of daily alerts (once per subscriber, never restarted).
create or replace function confirm_subscription(token uuid) returns boolean
language sql security definer set search_path = public as $$
  with u as (
    update subscribers
       set status = 'active', trial_ends_at = coalesce(trial_ends_at, now() + interval '14 days')
     where unsub_token = token and status = 'pending'
    returning 1)
  select exists (select 1 from u);
$$;
revoke all on function confirm_subscription(uuid) from public;
grant execute on function confirm_subscription(uuid) to anon;

-- ---------------------------------------------------------------- cold outreach
-- Suppliers named on public contract award notices. Only the engine (service key) can read or write this.
create table if not exists outreach_contacts (
  id uuid primary key default gen_random_uuid(),
  company_number text not null unique check (company_number ~ '^[A-Z0-9]{8}$'),
  company_name text,
  email text check (length(email) <= 254),
  contact_name text check (length(contact_name) <= 120),
  region text,
  pack text,
  source_ocid text,
  award_title text,
  company_type text,
  status text not null default 'new'
    check (status in ('new', 'checked', 'skipped', 'emailed', 'followed_up', 'replied', 'opted_out', 'bounced', 'converted')),
  skip_reason text,
  subject text,
  emailed_at timestamptz,
  followed_up_at timestamptz,
  responded_at timestamptz,
  optout_token uuid not null default gen_random_uuid(),
  created_at timestamptz not null default now()
);
create index if not exists outreach_contacts_status on outreach_contacts (status);
alter table outreach_contacts enable row level security;
revoke all on outreach_contacts from anon, authenticated;

-- Opt-out link in every outreach email: works with the secret token, reveals nothing.
create or replace function outreach_optout(token uuid) returns boolean
language sql security definer set search_path = public as $$
  with u as (update outreach_contacts set status = 'opted_out', responded_at = coalesce(responded_at, now())
              where optout_token = token returning 1)
  select exists (select 1 from u);
$$;
revoke all on function outreach_optout(uuid) from public;
grant execute on function outreach_optout(uuid) to anon;

-- Retention: drop contacts we never heard back from after 12 months. Opt-outs and bounces are kept
-- so we never email them again (suppression list).
create or replace function prune_outreach() returns void language sql as $$
  delete from outreach_contacts
   where status in ('new', 'checked', 'skipped', 'emailed', 'followed_up')
     and created_at < now() - interval '12 months';
$$;
revoke all on function prune_outreach() from public, anon, authenticated;
