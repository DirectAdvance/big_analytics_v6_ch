# PBI refresh optimization plan

Date: 2026-07-31

## Safety rule

Do not run unbounded `CREATE TABLE AS SELECT` over large facts. Large builders must use:

- daily batches;
- `SAFE_QUERY_SETTINGS` from `config/ch_utils.py`;
- shadow tables + `swap_shadow`;
- no per-batch `count()` over the growing target;
- no global `row_number()` windows over large datasets.

## Physical import layers

Implemented in code, to be run only after ClickHouse is healthy:

- `ad_analytics.pbi_import_big_analytics_full` -> `pbi_big_analytics_full` view.
- `ad_analytics.pbi_import_fact_direct_feed_funnel` -> `arf_fact` and `bi_fact_direct_feed_funnel`.
- `ad_analytics.pbi_import_region_spend` -> `bi_fact_region_spend`.
- `ad_analytics.pbi_import_fact_direct_feed_funnel` is physical; Direct placement import reads
  the compatibility `fact_direct_feed_funnel` view over `fact_direct_feed_funnel_light`.

These layers keep existing Power BI object names stable and avoid recomputing heavy views during refresh.

## Maximum star candidates

Implemented as optional builders, without removing columns from current PBI
facts:

```bash
.venv/bin/python3 star_refactor/build_star_extensions.py
```

This creates:

- `ad_analytics.Dim_AdFormat`
- `ad_analytics.Dim_AdNetworkType`
- `ad_analytics.Dim_Source`

They are not added to `_ALL_TABLES` yet because the current Power BI model does
not contain those tables. Add them to the semantic model first, validate
coverage, then register them for selective refresh.

### Feed / placement

Add or keep `Dim_PlacementFeed`.

Keys:

- `placement_feed_key`
- `placement_key`
- `feed_url_key`
- `feed_key`

Attributes:

- `placement`
- `feed_name`
- `feed_url`
- `ad_network_type`
- `AdNetworkType`

Fact should keep only:

- `date`
- `campaign_id`
- `ad_group_id` / `adgroup_id`
- `domain`
- `placement_feed_key`
- metrics: cost, clicks, impressions, all forms / CRM goals / funnel metrics

Remove from fact only after PBI remap and aggregate parity:

- `feed_name`
- `feed_url`
- `placement`
- campaign/adgroup names
- site descriptive columns

### Region spend

Use `Dim_Location` for:

- `id_location`
- `location`
- `Область`
- `GeoRegionType`
- `distance_km`
- `distance_km_agreg`

Fact should keep only:

- `date`
- `campaign_id`
- `ad_group_id`
- `domain`
- `id_location`
- `distance_km_agreg` if measures still need bucket filtering before PBI remap
- `ad_network_type`
- metrics: cost, clicks, impressions, CRM goals

Remove from fact only after PBI remap and aggregate parity:

- `location`
- `Область`
- `GeoRegionType`
- `distance_km`
- `campaign_name`
- `ad_group_name`
- `салон`
- `город`
- `регион`
- `тип_сайта`
- `шаблон`
- `статус`

### Criterion

Use `dim_criterion` for:

- `criterion`
- `criterion_type`
- `criterion_raw`

Fact should keep only:

- `date`
- `campaign_id`
- `ad_group_id`
- `criterion_key` or normalized `criterion`
- metrics

Remove descriptive criterion text from large spend facts only after model remap.

### Common dimensions

Already available or expected:

- `Dim_Date`
- `Dim_Campaign`
- `Dim_AdGroup`
- `Dim_Site`
- `Dim_Location`
- `Dim_PlacementFeed`
- `dim_criterion`

## Verification before removing columns

For each narrowed fact:

- row count by month must match source;
- sum cost/clicks/impressions by month must match source;
- funnel sums by month and attribution must match source;
- key coverage to Dim must be measured;
- Power BI field references must be remapped before removing old columns.

Metadata-only audit:

```bash
.venv/bin/python3 star_refactor/audit_pbi_sources.py
```

This reads only `system.tables`, `system.parts`, and `system.columns`; it does not scan large facts.

## Low-memory pipeline mode

For recovery/debug runs, skip spend/feed/star/PBI compatibility rebuilds:

```bash
.venv/bin/python3 pipeline.py --skip-heavy-pbi
```

The same mode can be enabled with:

```bash
PIPELINE_SKIP_HEAVY_PBI=1 .venv/bin/python3 pipeline.py
```

This keeps the default full pipeline unchanged, but gives a low-memory path for
main-fact troubleshooting after ClickHouse instability.

## BI/source parity sync, 2026-07-31

`step0_sync_local/step0.py` now syncs the v5/BI support tables that are needed
for crop targeting diagnostics and legacy PBI pages:

- `gsheets_crop_targeting_account`
- `gsheets_crop_targeting_account_leads`
- `gsheets_crop_targeting_account_pravilo_utm`
- `local_telega_in_orders`
- `local_telega_in_orders_errors`
- `crop_targeting_api_telegain_lead`
- `yandex_direct_cookie_analytics_website_pages`

`yandex_direct_cookie_analytics_website_pages` may already exist as an empty
compatibility `View`; step0 replaces that view with a physical `MergeTree`
snapshot from v5 `public`.

Post-change verification:

```bash
.venv/bin/python3 pipeline.py --only-step 0
```

Then check `system.tables` and row counts for the seven objects above before
running PBI compatibility rebuilds.

Verified on 2026-07-31:

- `pipeline.py --only-step 0` completed in 399.2 sec.
- PostgreSQL source counts matched ClickHouse active part counts:
  `gsheets_crop_targeting_account=1746`,
  `gsheets_crop_targeting_account_leads=1736`,
  `gsheets_crop_targeting_account_pravilo_utm=1857`,
  `local_telega_in_orders=1833`,
  `local_telega_in_orders_errors=2`,
  `crop_targeting_api_telegain_lead=1622`,
  `yandex_direct_cookie_analytics_website_pages=965764`.
- `yandex_direct_cookie_analytics_website_pages` is now a physical `MergeTree`,
  not the old empty compatibility `View`.

`step14_minus_snapshot` remains a placeholder in v6_ch, but now creates the
empty table with the full v5/BI-compatible schema (`has_minus`, `check_ok`,
`block`, `специалист`, counters). This prevents `bi_yandex_direct_minus_snapshot`
from crashing parity verification while the live Direct minus-snapshot fetch is
still not ported.

Verified on 2026-07-31:

- `pipeline.py --only-step 14` completed in 2.4 sec with
  `yandex_direct_minus_snapshot=0`.
- `data_check/verify_big_analytics.py --full --no-star` passed after the schema
  fix and step0 sync.

Current fast path note:

- `fast_pipeline.py` in this directory is still the old v5/PostgreSQL script and
  should not be used as a v6_ch validation path until it is ported.
- Use `pipeline.py --skip-heavy-pbi` for low-memory v6_ch validation. A
  2026-07-31 run completed the main pipeline path through step8 without
  ClickHouse memory runaway. In this mode step900 is called with `no_star=True`,
  because `fact_big_analytics` remains from the previous build when heavy
  star/PBI builders are skipped.

## Safe parallelization notes, 2026-07-31

Do not enable broad parallel execution in the default pipeline yet. The current
safe order is mostly dependency-bound:

- `step0 -> step1 -> step3/4 -> step6` must stay ordered for correctness.
  `step3` needs raw tables and campaign status, and `step6` needs corrected
  sources plus calls.
- Inside `step1`, `raw_yandex`, `raw_leads`, and `raw_calls` write independent
  tables, but parallelizing them should be capped at `max_parallel=2` only after
  another healthy baseline run; `raw_yandex` is already the largest stream.
- Heavy PBI builders (`region_spend`, `adformat_spend`, `criterion_spend`,
  `direct_feed_funnel`) write separate facts, but all read large Direct raw data.
  If parallelized, start with manual pairs and `SAFE_QUERY_SETTINGS`; do not run
  all four together on the current ClickHouse size.
- `build_star` and `build_pbi_compat` must remain after their upstream facts and
  should stay sequential.
- `step11` and `step13` rebuild shadow targets that feed later facts; keep them
  sequential.

## Batching audit, 2026-07-31

Checked heavy ClickHouse materialization code paths after the memory incident.

Daily-batched with `SAFE_QUERY_SETTINGS`, shadow tables, and no per-batch growing
target `count()`:

- `step1_load_raw`: `raw_yandex`, `raw_leads`, `raw_calls`.
- `step3_build_sources`: direct source and lead-based source batches.
- `step6_build_full`: `big_analytics_calls`, `big_analytics_full`.
- `step10_crop_targeting`: full-table overlay copy into `big_analytics_full_new`.
- `step11_pixel_score`: merge back into `big_analytics_full`.
- `step13_arrival`: `big_analytics_full_arrival`.
- `region_spend/build_region_spend.py`: `fact_region_spend`.
- `adformat_spend/build_adformat_spend.py`: `fact_adformat_spend`.
- `criterion_spend/build_criterion_spend.py`: `fact_criterion_spend`.
- `direct_feed_funnel/build.py`: `fact_direct_feed_funnel_light` plus compatibility view
  `fact_direct_feed_funnel`.
- `star_refactor/build_star.py`: `fact_big_analytics`, `fact_ml_korrektirovki`, `fact_vk_ads`.
- `star_refactor/build_pbi_compat.py`: `pbi_import_big_analytics_full`,
  `pbi_import_fact_direct_feed_funnel`, `pbi_import_region_spend`, `arp_fact`.
- `direct_feed_funnel/build.py`: `Dim_PlacementFeed` is rebuilt before swapping
  `fact_direct_feed_funnel`, so `--only-step=144` is self-contained.

Guardrails now applied centrally:

- `config/ch_utils.py` applies `SAFE_QUERY_SETTINGS` to metadata helpers, empty
  table creation, `replace_view`, `month_ranges_from_table`, and `count_rows`.
- `pipeline.py --skip-heavy-pbi` skips steps `141`, `142`, `143`, `1431`,
  `1432`, `144`, `145`, `146`; `--only-step` still allows one heavy builder to
  be run manually after ClickHouse is healthy.

Remaining non-batched read paths to watch:

- `step11_pixel_score._rebuild_pixel_score` still computes pixel weights as one
  bounded query with a window over pixel data. It is not a full-table copy, but
  it should be split by month if it trips `max_memory_usage`.
- `star_refactor/build_star.py` dimension builders `Dim_Date`, `Dim_Site`,
  `Dim_Campaign`, `Dim_AdGroup`, `Dim_Location` are one-shot aggregate CTAS.
  They create small dimensions, but read large facts. If ClickHouse remains
  memory-sensitive, convert these to staged daily aggregates before widening the
  star model further.
- `star_refactor/build_pbi_compat.py` creates some compatibility views and runs
  final `count_rows()` for logging. The counts are now bounded by safe settings;
  if a count over a view times out, replace it with `system.parts` row estimates
  for physical tables and skip view counts.

Fast local verification used for this pass:

```bash
python3 -m py_compile config/ch_utils.py pipeline.py refresh_powerbi.py step1_load_raw/step1.py step3_build_sources/step3.py step6_build_full/step6.py step10_crop_targeting/step10.py step11_pixel_score/step11.py step13_arrival/step13.py star_refactor/build_star.py star_refactor/build_pbi_compat.py star_refactor/build_star_extensions.py direct_feed_funnel/build.py region_spend/build_region_spend.py criterion_spend/build_criterion_spend.py adformat_spend/build_adformat_spend.py
git diff --check
rg -n "CREATE TABLE .* AS|CREATE TABLE \\{shadow\\}|CREATE TABLE ad_analytics\\.|INSERT INTO \\{shadow\\}|INSERT INTO ad_analytics\\." star_refactor/build_star.py star_refactor/build_pbi_compat.py
```

No live ClickHouse validation is included here; run it only after the cluster is
healthy, starting with `pipeline.py --skip-heavy-pbi`, then one heavy `--only-step`
at a time.
