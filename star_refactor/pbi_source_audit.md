# PBI Source Audit

| PBI table | CH object | engine | rows | disk | cols | attr cols | key/index note | recommendation |
|---|---|---|---:|---:|---:|---:|---|---|
| `big_analytics_full` | `big_analytics_full` | MergeTree | 5,243,403 | 282.1 MB | 73 | 11 | partition=toYYYYMM(ifNull(Date, toDate('2026-01-01'))); sort=ifNull(Date, toDate('2026-01-01')), ifNull(domain, ''), ifNull(_source_table, '') | star candidate: move text attrs to Dim_* |
| `big_analytics_full_arrival` | `big_analytics_full_arrival` | MergeTree | 73,007 | 6.1 MB | 73 | 11 | partition=toYYYYMM(ifNull(Date, toDate('2026-01-01'))); sort=ifNull(Date, toDate('2026-01-01')), ifNull(domain, ''), ifNull(_source_table, '') | dim candidate: repeated descriptive columns |
| `Dim_Date` | `Dim_Date` | MergeTree | 211 | 1.8 KB | 8 | 0 | partition=-; sort=Date | OK/low priority |
| `Dim_Campaign` | `Dim_Campaign` | MergeTree | 23,115 | 1.2 MB | 10 | 2 | partition=-; sort=ifNull(CampaignId, 0) | OK/low priority |
| `Dim_AdGroup` | `Dim_AdGroup` | MergeTree | 203,396 | 7.2 MB | 11 | 2 | partition=-; sort=ifNull(AdGroupId, 0) | OK/low priority |
| `Dim_Site` | `Dim_Site` | MergeTree | 1,725 | 74.3 KB | 15 | 7 | partition=-; sort=ifNull(domain, ''), ifNull(`салон`, '') | dim candidate: repeated descriptive columns |
| `fact_vk_ads` | `fact_vk_ads` | MergeTree | 30,527 | 668.7 KB | 22 | 5 | partition=toYYYYMM(date); sort=date, ifNull(account_id, 0), ifNull(ad_plan_id, 0), ifNull(ad_group_id, 0), ifNull(banner_id, 0), `атрибуция` | dim candidate: repeated descriptive columns |
| `analytics_report_placement` | `analytics_report_placement` | ReplacingMergeTree | view | view | 47 | 8 | partition=toYYYYMM(date); sort=date, row_hash | dim candidate: repeated descriptive columns |
| `direct_history` | `yandex_direct_history` | ReplacingMergeTree | 32,941 | 762.6 KB | 12 | 1 | partition=-; sort=login, campaign_id, event_type | OK/low priority |
| `check_utm_fuck_direct` | `check_utm_fuck_direct` | View | view | view | 12 | 2 | view source=? | review: complex view may recalc on refresh |
| `yandex_direct_korrektirovki` | `yandex_direct_korrektirovki` | View | view | view | 17 | 2 | view source=? | review: complex view may recalc on refresh |
| `yandex_direct_404_errors` | `yandex_direct_404_errors` | MergeTree | 13,548 | 687.4 KB | 14 | 1 | partition=toYYYYMM(ifNull(visit_date, toDate('2026-01-01'))); sort=ifNull(visit_date, toDate('2026-01-01')), ifNull(site, ''), ifNull(url, '') | OK/low priority |
| `yandex_direct_return_commission_report` | `yandex_direct_return_commission_report` | View | view | view | 13 | 0 | view source=? | review: complex view may recalc on refresh |
| `pixel_score` | `pixel_score` | MergeTree | 7,191 | 244.8 KB | 38 | 2 | partition=toYYYYMM(month); sort=month, ifNull(domain, ''), ifNull(`салон`, '') | OK/low priority |
| `yandex_direct_cookie_analytics_website_pages` | `yandex_direct_cookie_analytics_website_pages` | MergeTree | 965,764 | 33.3 MB | 23 | 0 | partition=-; sort=login_key, date_from, date_to, id | OK/low priority |
| `fact_adformat_spend` | `fact_adformat_spend` | MergeTree | 2,832,739 | 151.1 MB | 23 | 9 | partition=toYYYYMM(date); sort=date, campaign_id, ad_group_id, ifNull(ad_format, '') | star candidate: move text attrs to Dim_* |
| `fact_criterion_spend` | `fact_criterion_spend` | MergeTree | 4,578,648 | 289.7 MB | 20 | 8 | partition=toYYYYMM(date); sort=date, campaign_id, ad_group_id, ifNull(criterion_id, 0), criterion | star candidate: move text attrs to Dim_* |
| `fact_criterion_zayavki` | `fact_criterion_zayavki` | MergeTree | 121,504 | 6.9 MB | 24 | 5 | partition=toYYYYMM(created_date); sort=created_date, ifNull(campaign_id, 0), ifNull(criterion, '') | dim candidate: repeated descriptive columns |
| `dim_criterion` | `dim_criterion` | MergeTree | 90,810 | 1.9 MB | 3 | 2 | partition=-; sort=criterion | OK/low priority |
| `analytics_report_criterion` | `arc_fact` | MergeTree | 4,578,648 | 268.9 MB | 20 | 8 | partition=toYYYYMM(date); sort=date, campaign_id, ad_group_id, ifNull(criterion_id, 0), criterion | star candidate: move text attrs to Dim_* |
| `fact_region_spend` | `fact_region_spend` | MergeTree | 12,787,282 | 551.3 MB | 33 | 13 | partition=toYYYYMM(date); sort=date, campaign_id, ifNull(ad_group_id, 0), ifNull(id_location, 0) | star candidate: move text attrs to Dim_* |
| `fact_region_zayavki` | `fact_region_zayavki` | MergeTree | 165,221 | 8.1 MB | 25 | 6 | partition=toYYYYMM(created_date); sort=created_date, ifNull(campaign_id, 0), ifNull(id_location, 0) | dim candidate: repeated descriptive columns |
| `Dim_Location` | `Dim_Location` | MergeTree | 16,191 | 64.8 KB | 5 | 3 | partition=-; sort=id_location | OK/low priority |
| `Dim_PlacementFeed` | `Dim_PlacementFeed` | MergeTree | 33,169 | 1.9 MB | 8 | 3 | partition=-; sort=placement_feed_key | OK/low priority |
| `fact_direct_feed_funnel` | `fact_direct_feed_funnel` | MergeTree | 12,194,556 | 293.1 MB | 15 | 3 | partition=toYYYYMM(date); sort=date, campaign_id, ad_group_id, ifNull(placement, '') | OK/low priority |
| `analytics_report_feed` | `arf_fact` | MergeTree | 12,923,840 | 603.3 MB | 59 | 12 | partition=toYYYYMM(date); sort=date, campaign_id, adgroup_id, placement_feed_key | star candidate: move text attrs to Dim_* |
| `yandex_direct_minus_snapshot` | `yandex_direct_minus_snapshot` | MergeTree | 1,546 | 64.2 KB | 15 | 2 | partition=toYYYYMM(date); sort=date, login, campaign_id | OK/low priority |
| `v_yandex_direct_minus_delta` | `v_yandex_direct_minus_delta` | View | view | view | 16 | 2 | view source=? | review: complex view may recalc on refresh |
| `fact_ml_korrektirovki` | `fact_ml_korrektirovki` | MergeTree | 9,470 | 216.2 KB | 54 | 7 | partition=toYYYYMM(ifNull(Date, toDate('2026-01-01'))); sort=ifNull(Date, toDate('2026-01-01')), ifNull(RlAdjustmentId, 0), ifNull(domain, '') | dim candidate: repeated descriptive columns |

## Missing

- none
