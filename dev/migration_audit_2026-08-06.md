# BA v6 ClickHouse Migration Audit — 2026-08-06

## Короткий вывод

BA v6 сейчас не может считаться готовым к полному переезду только за счет запуска
`pipeline.py`: pipeline читает `raw_data.*`, но не синхронизирует эти таблицы с
Postgres и не проверяет их паритет. Главный блокер расхождения v5/v6 — сырой слой
ClickHouse, особенно CRMF-лиды и Direct raw.

Дополнительно найден и исправлен малый локальный дефект v6: `spec_fallback.py`
теперь классифицирует звонки не только по `campaign_code='звонки'`, но и по
фактическому `_source_table='calls'`. В полном прогоне `a460eeed0b83` это уже
сработало до `build_star`: step `spec_fallback` заполнил `23 546` пустых
`специалист`, включая `4 811` строк ветки `Звонки`.

## Обновление после полного прогона v6 и PBI-аудита

Свежий прогон v6:

- команда: `.venv/bin/python3 pipeline.py`;
- run_id: `a460eeed0b83`;
- сборка дошла до star/PBI compatibility;
- исходный `pipeline.py` завершился code `1` только из-за устаревшего verifier
  contract, который еще требовал удаленные legacy objects `arp_fact`/`arf_fact`/`arc_fact`;
- после синхронизации `data_check/verify_big_analytics.py` отдельный verifier прошел:
  `PASS`.

Финальные counts v6 после прогона:

- `raw_yandex=25 005 168`;
- `raw_leads=1 126 130`;
- `raw_calls=67 801`;
- `big_analytics_full=5 136 442`;
- `big_analytics_full_arrival=113 828`;
- `big_analytics_unified=5 250 270`;
- `fact_big_analytics=5 250 270`;
- `bi_pbi_big_analytics_full=5 250 270`;
- `bi_fact_direct_feed_funnel=12 801 544`;
- `bi_fact_region_spend=13 531 858`;
- `bi_fact_adformat_spend=2 974 999`;
- `bi_fact_criterion_spend=4 792 311`.

Power BI / ClickHouse inventory:

- в active admin semantic model 37 таблиц;
- 34 активных ClickHouse source;
- все 34 source существуют в `ad_analytics`;
- все 34 source являются `View`;
- локальные/calculated таблицы без CH source: `Users`, `Модель атрибуции`, `Dim_Distance`;
- после полного прогона legacy objects не восстановились:
  `analytics_report_placement`, `analytics_report_placement_v`, `arp_fact`, `arf_fact`,
  `arc_fact`, `bi_arp_fact`, `bi_arf_fact`, `bi_arc_fact`, `bi_Dim_Criterion`.

Отдельный PBI-аудит с финальными деталями: `dev/pbi_migration_audit_2026-08-06.md`.

Важно по v5 baseline:

- 2026-08-06 fresh `build_unified` в v5 доходил;
- все fresh `build_star` 2026-08-06 упали на `BUILD_STAR_DISK_GUARD`;
- последний успешный v5 `build_star`: `32cf5702`, `2026-08-05 09:57:43`;
- поэтому свежий compare v5/v6 использует stale v5 star baseline.

Свежий `data_check/compare/run.py` после прогона v6:

- v5: `public.fact_big_analytics`, rows `4 153 138`, max Date `2026-07-31`;
- v6: `ad_analytics.fact_big_analytics`, rows `4 215 729`, max Date `2026-07-31`,
  run_id `a460eeed0b83`;
- результат: `FAIL`;
- основные открытые дельты v6 к v5:
  - расход: `-8 938 616.04` (`-0.77%`);
  - обращения: `-5 099.69` (`-1.18%`);
  - заявки: `-2 440.51` (`-0.97%`);
  - квал: `-2 235.04` (`-4.16%`);
  - приезд: `-545.04` (`-1.37%`);
  - продажи: `-56.00` (`-1.57%`).

Свежая raw-сверка подтвердила, что часть расхождений начинается до финальных витрин:

- `raw_yandex`: CH `25 005 168`; PG `public.raw_yandex` `0`;
- `raw_leads`: CH `1 126 130`; PG `public.raw_leads` `976 420`; PG `public.leads` `1 117 456`;
- `raw_calls`: CH `67 801`; PG `public.raw_calls` `73 602`;
- `raw_data.direct_campaigns`: CH `33 738`; PG `public.yandex_direct_history` `89 679`.

Active PBIP после правок:

- missing field refs excluding auto-date artifacts: `0`;
- legacy refs: `0`;
- stale user report refs перенесены на существующие dimensions:
  `Dim_Campaign.account_login`, `Dim_Adjustment.RlAdjustmentId_total`,
  `Dim_Location.Область`, `Dim_Source.источник`, `Dim_AdFormat.ad_format`.

## Raw ClickHouse vs Postgres

Postgres source = `ad_analytics.public.*`; v5 staging `ad_analytics_bi.public.raw_*`
сейчас пустой/транзитный и не является надежным эталоном.

### CRM leads

За период `created_date >= 2026-01-01`:

- PG `public.leads_all`: `1 249 152`.
- CH `raw_data.leads_all`: `1 204 768`.
- CH `raw_data.leads_all.id` не равен PG `leads_all.id`: пересечение по `id` = `0`.
  В CH это surrogate id, не исходный ключ.

Для источников с `source_record_id` паритет близкий:

- PG: `365 259` строк / `360 828` ключей.
- CH: `367 466` строк / `363 030` ключей.
- missing in CH: `1 082` строк, в основном свежий `rivendell_excel` за август.
- extra in CH: `3 289` строк, в основном `marcar_crm_excel`/`plex_excel` за январь.

Главная проблема — `crmf_excel`:

- PG `crmf_excel` за 2026: `883 893`.
- CH `crmf_excel` за 2026: `837 302`.
- У CRMF нет `source_record_id`, а `deal_type` в CH переинтерпретирован: много PG
  строк с пустым `deal_type`/`Кредит` превращаются в CH `deal_type='Заявка'`.
- Январский контроль продаж подтверждает потерю до ETL: PG `crmf_excel`
  `status='Купил'` = `595` строк / `565` телефонов, CH `raw_data.leads_all` =
  `495` / `465`, CH `ad_analytics.raw_leads` = `418` / `394`.

### Direct raw

PG `public.yandex_direct_manager_reports` и CH `raw_data.yandex_direct_report_rows`
расходятся уже на raw-уровне. Пример по месяцам:

- Январь: PG `3 623 466` rows / `175 411 930.76` total_cost;
  CH `3 662 438` rows / `166 224 229.59` total_cost.
- Февраль: CH rows `+21 158`, total_cost `-4 481 089.90`.
- Март: CH rows `+86 604`, total_cost `-1 021 738.11`.
- Июль: CH rows `+31 953`, total_cost `+3 877 568.08`.

`row_key` в CH и `row_hash` в PG не совпадают по формату: CH sample = SHA1-like
40 hex, PG sample = SHA256-like 64 hex.

### Other raw/reference tables

- `raw_data.gsheet_sites`: PG `4 999`, CH `4 893`; по `Авто|Авто` CH меньше на `95`.
- `raw_data.domains`: PG `5 165`, CH `4 864`.
- `raw_data.telega_in_orders`: PG `1 860`, CH `7 204`; CH не зеркало PG, похоже
  содержит исторические/множественные версии.
- `raw_data.gsheet_priezdi_marcar`: PG `2 466`, CH `2 277`.
- `raw_data.crm_status_mapping`: это не прямой порт `public.crm_statuses`;
  в CH `791` денормализованная строка, в PG `crm_statuses` `178`.

## ClickHouse object classification

Точно текущие/нужные:

- `fact_big_analytics`, `Dim_*`, `big_analytics_full`/`_arrival` compatibility views,
  `pbi_big_analytics_full`, `bi_*`, `fact_region_*`, `fact_criterion_*`,
  `fact_direct_feed_funnel`, `fact_vk_ads`, `fact_ml_korrektirovki`.
- `raw_data.*`, которые читает step0/step1/step3/step10/step13.
- CH-managed manual inputs: `local_pixel_config`, `local_pixel_price_history`,
  `gsheets_crop_targeting_account*`, `yandex_direct_cookie_analytics_website_pages`.
- `pbi_import_fact_direct_feed_funnel`: физический PBI import layer, используется
  активной `bi_fact_direct_feed_funnel`, не удалять как дубль.

Подозрительные/не для удаления без владельца:

- `ad_analytics.raw_perform_leads`: пустой placeholder, сейчас step1 создает empty.
- `ad_analytics.local_leads_all_pg`: stale fallback artifact (`1 125 588` строк,
  max `2026-08-01`), pipeline его не использует, пока `raw_data.leads_all` непустая.
- `ad_analytics.yd_search_query_report_master`: тяжелая таблица внешнего проекта
  `yd_SEARCH_QUERY_REPORT`, не часть BA v6 pipeline.

Удалено как подтвержденное legacy:

- `ad_analytics.analytics_report_placement`, `analytics_report_placement_v`;
- `arp_fact`, `arf_fact`, `arc_fact`;
- `bi_arp_fact`, `bi_arf_fact`, `bi_arc_fact`, `bi_Dim_Criterion`.

## Code change in this pass

Файл: `spec_fallback.py`.

Было: ступень fallback `Звонки` срабатывала только при
`campaign_code='звонки'`. Для v6 calls это условие мертвое, потому что звонки
имеют `_source_table='calls'`, а `campaign_code` может быть `NULL` в physical full
или `seo` через `Dim_Campaign` в compatibility view.

Стало: ступень fallback `Звонки` срабатывает при
`campaign_code='звонки' OR _source_table='calls'`.

Проверено:

- RED: `pytest tests/test_spec_fallback.py -q` падал на отсутствии
  `_source_table='calls'`.
- GREEN: `pytest tests/test_spec_fallback.py -q` прошел.
- Регрессия: `pytest tests/test_pbi_contract_lists.py tests/test_spec_fallback.py tests/data_check/test_compare_run.py tests/data_check/test_compare_report.py -q`
  прошел: `34 passed`.
- `py_compile spec_fallback.py star_refactor/build_pbi_compat.py refresh_powerbi.py data_check/verify_big_analytics.py tests/test_spec_fallback.py tests/test_pbi_contract_lists.py`
  прошел.

## Что нужно для полного переезда

1. Сделать `raw_data` управляемым контуром v6: либо найти/починить внешний loader,
   либо перенести sync PG/API/GSheets -> CH в `big_analytics_v6_ch`.
2. Добавить preflight guard: step0 должен проверять не только `count()>0`, а
   freshness и контрольные суммы raw-источников против канонического источника.
3. После исправления raw-sync прогнать полный pipeline с нуля и снова запустить:
   `data_check/verify_big_analytics.py` и `data_check/compare/run.py --json`.
4. Только после raw-паритета закрывать оставшиеся локальные v6-расхождения:
   arrival fallback (`big_analytics_full_arrival`) и row-level campaign fields для
   `CampaignId=0` compatibility views.
