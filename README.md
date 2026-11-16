# Bid Canary

Daily and weekly UK public sector tender alerts for every sector (marketing leads with cleaning).
Data: Find a Tender OCDS API (Open Government Licence v3.0).

## How it runs (all free tiers)

| Piece | Where | What |
|---|---|---|
| `site/` | GitHub Pages | Landing page, "check your area" tool, signup, unsubscribe, privacy, terms |
| `engine.py fetch` | GitHub Actions, hourly | Pulls new notices into Supabase, rebuilds `site/data/counts.json`, redeploys the site |
| `engine.py sync-stripe` | GitHub Actions, daily | Sets each subscriber's plan from active Stripe subscriptions |
| `engine.py digest` | GitHub Actions, daily 05:23 UTC | Paid: daily digest with summaries. Free: Mondays, one showcase tender + count of the rest |
| `outreach.py` | GitHub Actions, weekdays 08:41 UTC | Cold outreach to award-winning limited companies (off until approved; see below) |
| `supabase/schema.sql` | Supabase | Tables + row-level security (website can only insert a free signup or unsubscribe by token) |

Payments use Stripe Payment Links (no server). Customers sign up free first, then upgrade with
the same email; the daily sync matches them by email.

## Offer (migration 004)

* **14-day trial:** confirming a sign-up starts 14 days of the daily Starter digest (set in the database, so the
  website can't fake it). Afterwards one "trial ended" email, then the free weekly digest.
* **Annual plan:** £299/year. Stripe price ID in secret `STRIPE_PRICE_STARTER_ANNUAL`, Payment Link in
  `STRIPE_STARTER_ANNUAL_LINK` (the button appears on the site once that secret is set).
* **Founding members:** the first 50 paying customers are flagged `founding`; the daily sync emails the owners
  when the offer fills so it can come off the site and terms.
* **Referrals:** every digest has the subscriber's `?ref=` link. When a referred business pays, the daily sync
  emails the owners to apply one free month to both in Stripe (the Stripe key is read-only, so this is manual).

## Cold outreach (`outreach.py`)

The hourly fetch saves suppliers named on contract award notices (only those with a Companies House number
and an email). The weekday job then: checks each with Companies House (active Ltd/PLC/LLP only; webmail and
public-sector addresses skipped), reads the outreach mailbox for replies, opt-outs and bounces, and sends up to
`OUTREACH_DAILY_LIMIT` (default 25, hard cap 50, counted across every run that day) branded emails with a
plain-text version: one first email naming a live tender in their sector and area, and at most one follow-up
7 days later.

**Never the same people twice:** each company, address and email domain gets one first email and at most one
follow-up, ever. On sending, one-way codes of the email, domain and company number go into the permanent
`outreach_suppression` table, which the 12-month clean-up does not touch, so a company that wins another
contract later is not contacted again. Replies, opt-outs and bounces cancel the follow-up. Firms with any
subscriber at the same domain are never cold-emailed. Contacts are marked as emailed before the send, so a
failure means one email too few, never one too many. `test_engine.py` simulates 8 weeks of runs (three a day,
with a clean-up in between) to prove it.
It pauses itself if bounces pass 5% or opt-outs pass 4%. It never prints names or addresses (logs are public).

It sends nothing until **all** of these are set:

| Where | Name | Value |
|---|---|---|
| Variable | `OUTREACH_ENABLED` | `true` (set to anything else to stop at once) |
| Variable | `OUTREACH_APPROVED_VERSION` | the `TEMPLATE_VERSION` in outreach.py (now `v2`), after reading a dry-run preview |
| Variable | `OUTREACH_MAILBOX` | the outreach mailbox, ideally on a separate domain from support@ |
| Variable | `OUTREACH_SENDER_NAME` | e.g. `Oscar Rudd` |
| Variable | `COMPANY_LINE` | e.g. `Bid Canary is a trading name of Example Ltd, company number 12345678, registered in England and Wales` |
| Variable | `OUTREACH_PACKS` | sectors to contact, default `cleaning` (e.g. `cleaning,security,grounds,fm`) |
| Variable | `OUTREACH_DAILY_LIMIT` | optional, default 25 |
| Secret | `COMPANIES_HOUSE_API_KEY` | free key from developer.company-information.service.gov.uk |
| Secret | `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET` | Entra app with Microsoft Graph application permissions `Mail.Send` and `Mail.Read`, limited to the outreach mailbox with an Exchange application access policy |
| Secret | `POSTAL_ADDRESS` | already used by the digests |

Basic-password SMTP is being switched off by Microsoft, so the job uses Microsoft Graph (OAuth) instead.
Resend is not used for outreach: its acceptable use policy bans cold email.

To preview: Actions → Outreach → Run workflow with "dry run" ticked. Up to 3 sample emails go to
support@ only. To stop: set `OUTREACH_ENABLED` to `false`.

## Setup

See "Bid Canary — launch setup guide". Secrets go in GitHub → Settings → Secrets and variables → Actions,
never in this repository.

## Tests

    python test_engine.py

Uses a synthetic fixture and SQLite; no network needed.
