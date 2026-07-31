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
- `ad_analytics.arp_fact` is physical, built from `fact_direct_feed_funnel` in daily batches.

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
- `direct_feed_funnel/build.py`: `fact_direct_feed_funnel`.
- `star_refactor/build_star.py`: `fact_big_analytics`, `fact_ml_korrektirovki`, `fact_vk_ads`.
- `star_refactor/build_pbi_compat.py`: `pbi_import_big_analytics_full`,
  `pbi_import_fact_direct_feed_funnel`, `pbi_import_region_spend`, `arp_fact`.
- `star_refactor/build_pbi_compat.py`: `Dim_PlacementFeed` now uses daily staging,
  then final aggregation over the staged key set.

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
