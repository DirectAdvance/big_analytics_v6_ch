# PBI Source Audit

## 2026-08-03 v6_ch star safety decision

- `fact_big_analytics` remains the main Power BI fact and is rebuilt from `big_analytics_unified`.
- Safe to keep out of the main fact: `CampaignName`, `AdGroupName`, `AdNetworkType`, `Device`,
  `Campaign`/`AdGroup` coder split fields, `manager_login`, `День недели`, `week_start`.
- Not safe to reconstruct only by `domain`/`source_key`: `специалист`, `_source_table`,
  `направление`, `салон`, `город`, `регион`, `тип_сайта`, `шаблон`, `статус`, `проджект`,
  `id_салона`, `менеджер`, `Название crm`. Audit showed multiple values per domain and
  `source_key='контекст'` covers `direct/tp8/tp9/tp10`; these fields stay row-level in
  `fact_big_analytics` until a stricter correction-aware dimension key exists.
- Verified after rebuild: `fact_big_analytics=5,395,699`, `105.35 MiB`; golden Kuderko
  `25,422,797.96`, `sales=55`; `verify_big_analytics.py` PASS and covers all 39 `bi_*` views.

| PBI table | CH object | engine | rows | disk | cols | attr cols | key/index note | recommendation |
|---|---|---|---:|---:|---:|---:|---|---|
| `big_analytics_full` | `big_analytics_full` | MergeTree | 5,263,438 | 307.6 MB | 73 | 11 | partition=toYYYYMM(ifNull(Date, toDate('2026-01-01'))); sort=ifNull(Date, toDate('2026-01-01')), ifNull(domain, ''), ifNull(_source_table, '') | star candidate: move text attrs to Dim_* |
| `big_analytics_full_arrival` | `big_analytics_full_arrival` | MergeTree | 132,051 | 13.2 MB | 73 | 11 | partition=toYYYYMM(ifNull(Date, toDate('2026-01-01'))); sort=ifNull(Date, toDate('2026-01-01')), ifNull(domain, ''), ifNull(_source_table, '') | dim candidate: repeated descriptive columns |
| `Dim_Date` | `Dim_Date` | MergeTree | 213 | 1.8 KB | 8 | 0 | partition=-; sort=Date | OK/low priority |
| `Dim_Campaign` | `Dim_Campaign` | MergeTree | 23,217 | 1.2 MB | 10 | 2 | partition=-; sort=ifNull(CampaignId, 0) | OK/low priority |
| `Dim_AdGroup` | `Dim_AdGroup` | MergeTree | 204,380 | 7.3 MB | 14 | 2 | partition=-; sort=ifNull(AdGroupId, 0) | OK/low priority |
| `Dim_Site` | `Dim_Site` | MergeTree | 5,026 | 273.4 KB | 16 | 7 | partition=-; sort=site_key | dim candidate: repeated descriptive columns |
| `fact_vk_ads` | `fact_vk_ads` | MergeTree | 30,538 | 671.4 KB | 22 | 5 | partition=toYYYYMM(date); sort=date, ifNull(account_id, 0), ifNull(ad_plan_id, 0), ifNull(ad_group_id, 0), ifNull(banner_id, 0), `атрибуция` | dim candidate: repeated descriptive columns |
| `analytics_report_placement` | `analytics_report_placement` | ReplacingMergeTree | view | view | 47 | 8 | partition=toYYYYMM(date); sort=date, row_hash | dim candidate: repeated descriptive columns |
| `direct_history` | `yandex_direct_history` | ReplacingMergeTree | 33,077 | 765.8 KB | 12 | 1 | partition=-; sort=login, campaign_id, event_type | OK/low priority |
| `check_utm_fuck_direct` | `check_utm_fuck_direct` | View | view | view | 12 | 2 | view source=? | review: complex view may recalc on refresh |
| `yandex_direct_korrektirovki` | `yandex_direct_korrektirovki` | View | view | view | 17 | 2 | view source=? | review: complex view may recalc on refresh |
| `yandex_direct_404_errors` | `yandex_direct_404_errors` | MergeTree | 13,548 | 687.4 KB | 14 | 1 | partition=toYYYYMM(ifNull(visit_date, toDate('2026-01-01'))); sort=ifNull(visit_date, toDate('2026-01-01')), ifNull(site, ''), ifNull(url, '') | OK/low priority |
| `yandex_direct_return_commission_report` | `yandex_direct_return_commission_report` | View | view | view | 13 | 0 | view source=? | review: complex view may recalc on refresh |
| `pixel_score` | `pixel_score` | MergeTree | 459,881 | 11.5 MB | 38 | 2 | partition=toYYYYMM(month); sort=month, ifNull(domain, ''), ifNull(`салон`, '') | OK/low priority |
| `yandex_direct_cookie_analytics_website_pages` | `yandex_direct_cookie_analytics_website_pages` | MergeTree | 965,764 | 33.2 MB | 23 | 0 | partition=-; sort=login_key, date_from, date_to, id | OK/low priority |
| `fact_adformat_spend` | `fact_adformat_spend` | MergeTree | 2,919,893 | 49.1 MB | 10 | 0 | partition=toYYYYMM(date); sort=date, campaign_id, ad_group_id, ad_network_type_key, ifNull(ad_format, '') | OK/low priority |
| `fact_criterion_spend` | `fact_criterion_spend` | MergeTree | 4,710,663 | 97.0 MB | 11 | 0 | partition=toYYYYMM(date); sort=date, campaign_id, ad_group_id, ad_network_type_key, ifNull(criterion_id, 0), criterion_key | OK/low priority |
| `fact_criterion_zayavki` | `fact_criterion_zayavki` | MergeTree | 128,479 | 7.9 MB | 24 | 5 | partition=toYYYYMM(created_date); sort=created_date, ifNull(campaign_id, 0), ifNull(criterion, '') | dim candidate: repeated descriptive columns |
| `dim_criterion` | `dim_criterion` | MergeTree | 92,145 | 4.9 MB | 4 | 2 | partition=-; sort=criterion_key | OK/low priority |
| `analytics_report_criterion` | `arc_fact` | View | view | view | 47 | 12 | view source=? | review: complex view may recalc on refresh |
| `fact_region_spend` | `fact_region_spend` | MergeTree | 13,248,638 | 170.6 MB | 20 | 4 | partition=toYYYYMM(date); sort=date, campaign_id, ifNull(ad_group_id, 0), ad_network_type_key, ifNull(id_location, 0) | OK/low priority |
| `fact_region_zayavki` | `fact_region_zayavki` | MergeTree | 175,232 | 8.6 MB | 25 | 6 | partition=toYYYYMM(created_date); sort=created_date, ifNull(campaign_id, 0), ifNull(id_location, 0) | dim candidate: repeated descriptive columns |
| `Dim_Location` | `Dim_Location` | MergeTree | 16,227 | 64.9 KB | 5 | 3 | partition=-; sort=id_location | OK/low priority |
| `Dim_PlacementFeed` | `Dim_PlacementFeed` | MergeTree | 33,879 | 2.0 MB | 8 | 3 | partition=-; sort=placement_feed_key | OK/low priority |
| `fact_direct_feed_funnel` | `fact_direct_feed_funnel` | MergeTree | 12,573,546 | 196.2 MB | 12 | 0 | partition=toYYYYMM(date); sort=date, campaign_id, ad_group_id, placement_feed_key | OK/low priority |
| `analytics_report_feed` | `arf_fact` | View | view | view | 29 | 0 | view source=? | review: complex view may recalc on refresh |
| `yandex_direct_minus_snapshot` | `yandex_direct_minus_snapshot` | MergeTree | 1,546 | 64.2 KB | 15 | 2 | partition=toYYYYMM(date); sort=date, login, campaign_id | OK/low priority |
| `v_yandex_direct_minus_delta` | `v_yandex_direct_minus_delta` | View | view | view | 16 | 2 | view source=? | review: complex view may recalc on refresh |
| `fact_ml_korrektirovki` | `fact_ml_korrektirovki` | MergeTree | 10,121 | 236.6 KB | 54 | 7 | partition=toYYYYMM(ifNull(Date, toDate('2026-01-01'))); sort=ifNull(Date, toDate('2026-01-01')), ifNull(RlAdjustmentId, 0), ifNull(domain, '') | dim candidate: repeated descriptive columns |

## Missing

- none
