# PBI Source Audit

## 2026-08-14 v6_ch direct placement light fact

- Step144 `direct_feed_funnel.build` now treats the old feed-funnel name as a compatibility
  surface for the Direct placement aggregate.
- Physical storage is `ad_analytics.fact_direct_feed_funnel_light`; it keeps
  `placement_feed_key_hash` and metric columns. `ad_analytics.fact_direct_feed_funnel` is a
  compatibility view that restores `placement_feed_key` through `Dim_PlacementFeed`.
- `Dim_PlacementFeed` is rebuilt inside step144 before the compatibility view is swapped, so
  `--only-step=144` does not depend on a later PBI compatibility step.
- Verified after rerun: `fact_direct_feed_funnel_light=13,246,925`,
  `fact_direct_feed_funnel=13,246,925`, `Dim_PlacementFeed=34,929`.

## 2026-08-08 v6_ch account/CRM/salon star cleanup

- `Dim_Account`, `Dim_CRMStatus`, `Dim_Salon` promoted to first-class star dimensions:
  `Dim_Account=1,226`, `Dim_CRMStatus=206`, `Dim_Salon=4,146`.
- `fact_big_analytics` now keeps only dimension keys for these groups:
  `account_key`, `crm_status_key`, `salon_key`. Duplicated text attributes such as
  `account_login`, `Название crm`, `тип_заявки`, `статус`, `cascade_level`, `салон`,
  `город`, `регион`, `тип_сайта`, `шаблон`, `специалист`, `проджект`, `менеджер`,
  `id_салона`, `направление`, `direction` are removed from the physical fact.
- Power BI/backward compatibility is preserved through `pbi_*`, `bi_*`, and wide
  compatibility views. `pbi_big_analytics_full`, `big_analytics_full`,
  `big_analytics_full_arrival`, `big_analytics_pixel_score`, and `big_analytics_unified`
  restore the old text columns through `Dim_Account`/`Dim_CRMStatus`/`Dim_Salon` joins.
- Pipeline order was corrected for the narrower fact: `145 build_star` builds required dims
  before swapping the fact, `1451 build_star_extensions` is idempotent after the wide source is
  removed, `148 cleanup_wide_intermediates` rebuilds wide compatibility views, then `146
  build_pbi_compat` builds PBI objects.
- Verified post-run counts: `fact_big_analytics=5,309,571`, `pbi_big_analytics_full=5,309,571`,
  `big_analytics_full=5,189,705`, `big_analytics_full_arrival=119,866`,
  `big_analytics_pixel_score=234,643`, `big_analytics_unified=5,309,571`,
  `pixel_score=234,643`.
- `data_check.verify_big_analytics` golden now reads Kuderko `специалист` through `Dim_Salon`
  and verifies that PBI compatibility still exposes the restored text columns. `step900` passed;
  the existing `KUDERKO_RAW_INCOMPLETE` warning remains informational while raw Direct history is
  incomplete for 38 of 67 Kuderko logins.
- Measured runtime for the clean analytical path from `--from-step 3` is about `54m52s` by summed
  successful step durations. The debug/recovery wall clock for this migration run was `1h08m43s`.

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

## 2026-08-03 wide cleanup result

- `big_analytics_full`, `big_analytics_full_arrival`, `big_analytics_pixel_score`,
  `big_analytics_unified` are now compatibility `View` objects backed by
  `fact_big_analytics` + `Dim_*` joins.
- `big_analytics_sources` is no longer kept as a durable table after the star build. It remains
  only an upstream staging object when earlier pipeline steps are rerun.
- Current post-cleanup counts: `big_analytics_full=5,263,651`,
  `big_analytics_full_arrival=132,048`, `big_analytics_pixel_score=459,849`,
  `big_analytics_unified=5,395,699`, `fact_big_analytics=5,395,699`.
- Final verifier enforces this shape: wide compatibility objects must exist as `View`, and
  `big_analytics_sources` is not required.

## 2026-08-03 raw/Postgres count check

- Direct read-only counts against Victory Postgres `ad_analytics` were checked after the CH
  cleanup. They are not a strict pass/fail gate for the star cleanup because current
  `raw_data` is not a simple 1:1 live mirror for every table.
- Observed counts: `domains` PG `5,164` vs CH `4,864`; `leads_all` PG `1,780,537`
  vs CH `1,931,835`; `campaigns` PG `21,586` vs CH `direct_campaigns=33,077`;
  `metrika_goals` PG `1,067` vs CH `metrika_yandex_goals=28,914`;
  `yandex_direct_manager_reports` PG `24,309,710` vs CH
  `yandex_direct_report_rows=24,513,866`.
- `raw_data.migration_checkpoints` exists and has reconciled historical checkpoints for
  `domains`, `leads_all`, `metrika_yandex_goals`, but some entries are stale/partial for current
  live counts, for example Direct report checkpoint is `0/0` while CH raw has `24,513,866` rows.

## 2026-08-04 dimension naming cleanup

- Criterion dimension now follows the same convention as the other dimensions:
  physical `Dim_Criterion` (`MergeTree`, `92,145` rows).
- Legacy `dim_criterion` remains as a compatibility `View` over `Dim_Criterion` so existing PBI
  model references and old SQL keep working during transition.
- Both BI-facing views are present and covered by verifier: `bi_Dim_Criterion=92,145` and
  `bi_dim_criterion=92,145`.

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
| `Dim_Criterion` | `Dim_Criterion` | MergeTree | 92,145 | 4.9 MB | 4 | 2 | partition=-; sort=criterion_key | OK/low priority |
| `dim_criterion` | `dim_criterion` | View | view | view | 4 | 2 | compatibility view over `Dim_Criterion` | legacy PBI/sql alias |
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
