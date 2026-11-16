-- Migration 002: double opt-in. Run once in Supabase > SQL Editor.
alter table subscribers add column if not exists confirm_sent_at timestamptz;
alter table subscribers drop constraint if exists subscribers_status_check;
alter table subscribers add constraint subscribers_status_check check (status in ('pending', 'active', 'unsubscribed'));
alter table subscribers alter column status set default 'pending';

drop policy if exists "public free signup" on subscribers;
create policy "public free signup" on subscribers for insert to anon
  with check (plan = 'free' and status = 'pending');

create or replace function confirm_subscription(token uuid) returns boolean
language sql security definer set search_path = public as $$
  with u as (update subscribers set status = 'active' where unsub_token = token and status = 'pending' returning 1)
  select exists (select 1 from u);
$$;
revoke all on function confirm_subscription(uuid) from public;
grant execute on function confirm_subscription(uuid) to anon;
