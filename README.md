# Bid Canary

Daily and weekly UK public sector tender alerts for cleaning, security, grounds and FM firms.
Data: Find a Tender OCDS API (Open Government Licence v3.0).

## How it runs (all free tiers)

| Piece | Where | What |
|---|---|---|
| `site/` | GitHub Pages | Landing page, "check your area" tool, signup, unsubscribe, privacy, terms |
| `engine.py fetch` | GitHub Actions, hourly | Pulls new notices into Supabase, rebuilds `site/data/counts.json`, redeploys the site |
| `engine.py sync-stripe` | GitHub Actions, daily | Sets each subscriber's plan from active Stripe subscriptions |
| `engine.py digest` | GitHub Actions, daily 05:23 UTC | Paid: daily digest with summaries. Free: Mondays, top 5 |
| `supabase/schema.sql` | Supabase | Tables + row-level security (website can only insert a free signup or unsubscribe by token) |

Payments use Stripe Payment Links (no server). Customers sign up free first, then upgrade with
the same email; the daily sync matches them by email.

## Setup

See "Bid Canary — launch setup guide". Secrets go in GitHub → Settings → Secrets and variables → Actions,
never in this repository.

## Tests

    python test_engine.py

Uses a synthetic fixture and SQLite; no network needed.
