# CLAUDE.md — big_analytics_v6_ch

> Navigation and hard rules only. Heavy details are lazy-loaded from linked docs. Keep ≤150 lines.
> This is the ClickHouse fork of `work/big_analytics_v5/`.

## Status

- ETL is migrated to ClickHouse and runs from Victory as
  `~/venv-v6/bin/python3 ~/big_analytics_v6_ch/pipeline.py`.
- VictoryAds dashboard `/dashboard/scripts` launches v6 via `server=victory_v6` through SSH alias
  `victory`; no separate `yandex` SSH host is used.
- Victory stores only code, `~/venv-v6`, and rotated logs. Data, marts, raw layers, and heavy caches
  live in Yandex Cloud ClickHouse, not on Victory disk.
- Daily run is scheduled on Victory: `0 4 * * *` UTC = 09:00 Yekaterinburg. It runs `cron_run.py`,
  not `pipeline.py` — the pipeline itself sends no Telegram at all, so the wrapper reports the
  outcome. Night pipeline is also scheduled: `10 18 * * *` UTC = 23:10 Yekaterinburg via
  `step_cron_night/pipeline_night.py` with `/tmp/ba6_night.lock`.
- Victory `~/big_analytics_v5/` is the old v5 production contour, not this project.
- Current ClickHouse storage: Yandex Cloud
  `rc1b-q7j2ie10fdverqrk.mdb.yandexcloud.net:8443`, DBs `ad_analytics` and `raw_data`.
- PostgreSQL legacy code is in `archive/postgres_legacy_2026_07_31/` and is not in the pipeline.
- One live PostgreSQL dependency remains: step3 `_fetch_reviews_rows_from_postgres` reads
  `yandex_direct_raw.yandex_direct_reports_reviews` from Victory PG — reviews are absent from
  `raw_data`.
- BA6 operational core is accepted after run `ed6bfc6f9c23` (2026-08-20). As of 2026-08-27 the
  feed funnel is rebuilt from `raw_data.direct_feed_report_rows`; `bi_analytics_report_placement`
  is a separate RSYA placement view over `raw_data.yandex_direct_report_rows` (`PBI_TABLES.md` §0).
- Freshness must be read from table dates, never from `raw_data.etl_runs` or
  `leads_all.updated_at` — both are stale by design (`KNOWN_ISSUES.md` #43).
- Latest status, open v5↔v6 deltas, and run ids live in `STATE.md` and `KNOWN_ISSUES.md`.

## Read First

| Need | File |
|---|---|
| File/router map | `INDEX.md` |
| Current handoff | `STATE.md`; old entries `STATE_ARCHIVE.md` |
| Migration plan/status | `PLAN.md`, `SPEC.md` |
| Architecture/source contracts | `PROJECT_CHARTER.md` |
| Attribution authority | `ATTRIBUTION.md` |
| Golden numbers and SQL | `GOLDEN_BASELINE.md` |
| Pipeline map and step ownership | `PIPELINES.md` |
| Operations and recovery | `RUNBOOK.md` |
| Useful SQL | `QUERIES.md` |
| Table lifecycle | `DB_TABLES.md` |
| Power BI compatibility/source tables | `PBI_TABLES.md` |
| Known defects and accepted deltas | `KNOWN_ISSUES.md` |
| v5↔v6 raw comparison | `RAW_DIFF_FINDINGS.md` |
| v5↔v6 Power BI parity (31 tables) | `PBI_TABLES.md` §0 |
| Data quality checks | `data_check/README.md`, `data_verification/README.md` |
| Column dictionary | `COLUMNS_big_analytics_full.md` |
| Canonical values | `CANON.md`, `FUNNEL.md` |
| Technical correction blocks | `BLOCKS.md` |
| Cookies | `COOKIES.md` |
| Post-loop/star/PBI notes | `STAR_REFACTOR_BRIEF.md` |
| Step-specific docs | `step<N>_*/CLAUDE.md` + `README.md` |

## Hard Data Invariants

- Golden baseline is mandatory for pipeline/attribution/funnel/star changes. In v6_ch the current
  tolerance is documented in `GOLDEN_BASELINE.md`; v5 tolerance remains separate.
- Run `python3 data_check/verify_big_analytics.py` for the default fast gate. It returns
  `0=PASS`, `1=FAIL`, `2=crash`.
- Do not add a new golden/invariant block to `verify_big_analytics.py` without asking Semyon first.
- Fractional pixel attribution must never be cast to `int` per row. Round only final sums.
- `big_analytics_full.источник` must not be NULL; `"Date" >= '2026-01-01'`.
- BA6 Power BI covers only niche `Авто`. Non-auto niches are not analyzed in BI and must not be
  added to BI dimensions/pages to explain orphan domain spend.
- Funnel nesting: `korr ≥ kval ≥ priezd ≥ prodazhi`, `credit ≥ approved`.
- No double lead accounting: `direct ∩ crop_targeting = 0`.
- The specialist column is `"специалист"`, not `директолог`.

## ClickHouse Discipline

- All active ETL should go through `config/ch_db.py` (`clickhouse-connect`) and
  `load_db('victory_clickhouse')`.
- PostgreSQL v5 code in `archive/postgres_legacy_2026_07_31/` is reference only.
- SQL with Mac-local ClickHouse access should use `get_client()` / `get_client('raw_data')` or MCP
  `clickhouse-victory`.
- Before a full pipeline run for SQL fixes, do cheap validation first: read block order, run
  SELECT-equivalent/live SQL, and confirm step ordering.
- Full pipeline is final confirmation, not the first debugging tool for every SQL edit.
- If a PBI/star table is a simple projection without joins/aggregations, prefer VIEW over TABLE.
- Incremental refresh is forbidden. PBI partitioning by `_source_table` is allowed only for
  parallel loading, not incremental refresh.

## Run Commands

```bash
cd work/big_analytics_v6_ch
.venv/bin/python3 pipeline.py
.venv/bin/python3 pipeline.py --only-step=3
.venv/bin/python3 pipeline.py --from-step=12
.venv/bin/python3 pipeline.py --include-maintenance
.venv/bin/python3 data_check/verify_big_analytics.py [--full]
```

Full command map, timing, and maintenance steps are in `PIPELINES.md`.

## Power BI / PBIP

- Edit only PBIP `v00` with embedded model unless Semyon explicitly says otherwise.
- Do not edit thin reports or `.pbix` binaries for semantic model/DAX work.
- Verify actual changed DAX/TMDL objects and that neighboring measures are untouched.
- Re-read root-owned files after writes; permissions can make edits silently fail.
- PBI compatibility and source-table rules are in `PBI_TABLES.md` and `STAR_REFACTOR_BRIEF.md`.

## Git

- Separate nested git repo: `https://github.com/DirectAdvance/big_analytics_v6_ch.git`.
- `CLAUDE.md` in this repo is `.gitignore`-ignored; still keep it accurate locally.
- Triggers: "гит/камит/коммит v6_ch", "отправь на гит", "запушь".
- Use Conventional Commits in Russian: `feat:`, `fix:`, `refactor:`, `docs:`, `data:`.
- Before commit: `git status`; no `.secret/`, logs, CSV dumps, tokens, or cookies.
- Do not commit, push, or force-push without explicit user command.
