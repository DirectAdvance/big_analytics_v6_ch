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
