# direct_account_reviews — v6 ClickHouse (weekly)

Активный шаг: `run.py` (night step 107, weekly-only — see `step_cron_night/README.md`).

CH-native replacement for the old BA5 weekly PostgreSQL job. Two sub-steps run in sequence:

- `load_reviews.py` — Google Sheets "Power BI" A:E → `ad_analytics.yandex_direct_account_reviews`
  (full `TRUNCATE` + reload every run).
- `fetch_direct_stats.py` — Yandex Direct Reports API v5 → `ad_analytics.yandex_direct_reports_reviews`,
  incremental per account (`max(Date) - 7 days` .. today; full history only for a never-seen
  account). This is the ~40 min bottleneck: the Reports API queue (HTTP 201/202) forces a
  `sleep`/retry loop per account/token.

`backfill_from_postgres.py` — one-off, already run 2026-08-24 to seed 2026-01-01..2026-08-16
history from the frozen Victory PostgreSQL `yandex_direct_raw.*` pair (the old v5 weekly cron was
disabled, so that pair never gets fresher). Do not run again once the weekly cron is live.

Old v5/PostgreSQL scripts (Sheets/API logic reference only, do not run — write to `public.*` on
Victory PG): `archive/postgres_legacy_2026_07_31/step_cron_night/direct_account_reviews/`.

## Reconciliation — do not compare with a flat date cutoff

`fetch_direct_stats.py` re-pulls `max(Date) - SAFETY_DAYS (7) .. today` per login every run,
and Yandex Direct restates figures retroactively inside that window — this is legitimate,
not a defect. When comparing a run against a baseline (e.g. the 2026-08-24 PG->CH reviews
migration baseline):

- `Date` strictly outside the last run's re-pull window (7+ days before that run's `today`)
  MUST match the baseline exactly. Any movement there is a real bug.
- `Date` inside that window MAY legitimately differ — quantify the delta, attribute it to
  API re-statement, don't treat it as a pass/fail criterion on its own.

For the 2026-08-16 backfill baseline specifically: `Date <= '2026-08-08'` must be unchanged;
`2026-08-09..2026-08-16` is the re-pull window and is expected to move. See
`fetch_direct_stats.py` module docstring for the general rule (SAFETY_DAYS, not this fixed
date, is the source of truth going forward).

## Check

```bash
python3 step_cron_night/pipeline_night.py --only-step 107 --no-tg
```

```sql
SELECT count(), sum(`Cost`), min(`Date`), max(`Date`)
FROM ad_analytics.yandex_direct_reports_reviews;

SELECT count(), count(DISTINCT `аккаунт`)
FROM ad_analytics.yandex_direct_account_reviews;
```
