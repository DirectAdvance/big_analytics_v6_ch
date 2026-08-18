# PBI Source Audit

| PBI table | CH object | engine | rows | disk | cols | attr cols | key/index note | recommendation |
|---|---|---|---:|---:|---:|---:|---|---|
| `big_analytics_full` | `pbi_big_analytics_full` | View | view | view | 42 | 7 | view source=pbi_import_big_analytics_full | OK: view over physical import |
| `big_analytics_full_arrival` | `big_analytics_full_arrival` | View | view | view | 73 | 11 | view source=fact_big_analytics | star/materialize candidate: source fact_big_analytics |
| `Dim_Date` | `bi_Dim_Date` | View | view | view | 8 | 0 | view source=Dim_Date | OK: projection view over Dim_Date |
| `Dim_Campaign` | `bi_Dim_Campaign` | View | view | view | 14 | 3 | view source=yandex_direct_korrektirovki | OK: projection view over yandex_direct_korrektirovki |
| `Dim_AdGroup` | `bi_Dim_AdGroup` | View | view | view | 15 | 2 | view source=Dim_AdGroup | OK: projection view over Dim_AdGroup |
| `Dim_Site` | `bi_Dim_Site` | View | view | view | 15 | 7 | view source=Dim_Site | star/materialize candidate: source Dim_Site |
| `fact_vk_ads` | `bi_fact_vk_ads` | View | view | view | 19 | 4 | view source=fact_vk_ads | OK: projection view over fact_vk_ads |
| `direct_history` | `yandex_direct_history` | View | view | view | 12 | 1 | view source=? | review: complex view may recalc on refresh |
| `check_utm_fuck_direct` | `bi_check_utm_fuck_direct` | View | view | view | 9 | 0 | view source=check_utm_fuck_direct | OK: projection view over check_utm_fuck_direct |
| `yandex_direct_korrektirovki` | `bi_yandex_direct_korrektirovki` | View | view | view | 14 | 0 | view source=yandex_direct_korrektirovki | OK: projection view over yandex_direct_korrektirovki |
| `yandex_direct_404_errors` | `bi_yandex_direct_404_errors` | View | view | view | 14 | 1 | view source=yandex_direct_404_errors | OK: projection view over yandex_direct_404_errors |
| `pixel_score` | `bi_pixel_score` | View | view | view | 35 | 0 | view source=pixel_score | star/materialize candidate: source pixel_score |
| `yandex_direct_cookie_analytics_website_pages` | `bi_yandex_direct_cookie_analytics_website_pages` | View | view | view | 23 | 0 | view source=yandex_direct_cookie_analytics_website_pages | OK: projection view over yandex_direct_cookie_analytics_website_pages |
| `fact_adformat_spend` | `bi_fact_adformat_spend` | View | view | view | 15 | 0 | view source=fact_adformat_spend | OK: projection view over fact_adformat_spend |
| `fact_criterion_spend` | `bi_fact_criterion_spend` | View | view | view | 22 | 1 | view source=fact_criterion_spend | OK: projection view over fact_criterion_spend |
| `fact_criterion_zayavki` | `bi_fact_criterion_zayavki` | View | view | view | 16 | 1 | view source=fact_criterion_zayavki | OK: projection view over fact_criterion_zayavki |
| `dim_criterion` | `bi_dim_criterion` | View | view | view | 3 | 2 | view source=Dim_Criterion | OK: projection view over Dim_Criterion |
| `fact_region_spend` | `bi_fact_region_spend` | View | view | view | 17 | 1 | view source=fact_region_spend | OK: projection view over fact_region_spend |
| `fact_region_zayavki` | `bi_fact_region_zayavki` | View | view | view | 17 | 0 | view source=fact_region_zayavki | OK: projection view over fact_region_zayavki |
| `Dim_Location` | `bi_Dim_Location` | View | view | view | 5 | 3 | view source=Dim_Location | OK: projection view over Dim_Location |
| `Dim_PlacementFeed` | `bi_Dim_PlacementFeed` | View | view | view | 10 | 3 | view source=Dim_PlacementFeed | OK: projection view over Dim_PlacementFeed |
| `fact_direct_feed_funnel` | `bi_fact_direct_feed_funnel` | View | view | view | 29 | 0 | view source=pbi_import_fact_direct_feed_funnel | OK: view over physical import |
| `yandex_direct_minus_snapshot` | `bi_yandex_direct_minus_snapshot` | View | view | view | 13 | 0 | view source=yandex_direct_minus_snapshot | OK: projection view over yandex_direct_minus_snapshot |
| `v_yandex_direct_minus_delta` | `bi_v_yandex_direct_minus_delta` | View | view | view | 14 | 0 | view source=v_yandex_direct_minus_delta | OK: projection view over v_yandex_direct_minus_delta |
| `fact_ml_korrektirovki` | `bi_fact_ml_korrektirovki` | View | view | view | 34 | 0 | view source=fact_ml_korrektirovki | star/materialize candidate: source fact_ml_korrektirovki |

## Missing

- none
