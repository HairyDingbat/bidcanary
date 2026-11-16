-- Migration 003: any sector + custom keywords. Run once in Supabase > SQL Editor.
alter table subscribers drop constraint if exists subscribers_pack_check;
alter table subscribers add constraint subscribers_pack_check check (pack ~ '^[a-z]{2,20}$');
alter table subscribers add column if not exists keywords text check (length(keywords) <= 200);
grant insert (email, pack, regions, keywords, consent_text) on subscribers to anon;
