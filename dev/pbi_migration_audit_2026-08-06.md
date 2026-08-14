# Big Analytics v6 CH / Power BI migration audit — 2026-08-06

## Область работ

- Работа велась только на стороне `big_analytics_v6_ch` и активных PBIP-файлов
  `/Users/semen/Documents/Отчеты_victory_Powerbi`.
- Внешний raw loader не менялся.
- PostgreSQL v5 читался только read-only.

## Активная Power BI модель

Активная admin semantic model:
`/Users/semen/Documents/Отчеты_victory_Powerbi/Большая аналитика_admin_ch/Большая аналитика_v00.SemanticModel`

Активный user report:
`/Users/semen/Documents/Отчеты_victory_Powerbi/Большая аналитика_user_ch/Большая аналитика_user.Report`

Итог:

- В active admin model 37 semantic tables.
- В active admin model 34 активных ClickHouse source.
- Все 34 активных source существуют в ClickHouse `ad_analytics`.
- Все 34 активных source являются `View`.
- Локальные/calculated model tables без CH source: `Users`, `Модель атрибуции`, `Dim_Distance`.

## Удаленные legacy ClickHouse objects

Удалены после проверки зависимостей:

- `analytics_report_placement`
- `analytics_report_placement_v`
- `arp_fact`
- `arf_fact`
- `arc_fact`
- `bi_arp_fact`
- `bi_arf_fact`
- `bi_arc_fact`
- `bi_Dim_Criterion`

Проверка после полного прогона:

- `LEGACY_FOUND []`
- `ACTIVE 34 FOUND 34 MISSING []`
- `NON_VIEW_ACTIVE []`

## Изменения кода

- `star_refactor/build_pbi_compat.py`
  - legacy `Dim_Criterion`, `arp_fact`, `arf_fact`, `arc_fact` убраны из BI view contract;
  - `arp_fact`, `arf_fact`, `arc_fact` больше не пересобираются;
  - физическая `Dim_Criterion` оставлена, потому что активная `bi_dim_criterion`
    зависит от `dim_criterion -> Dim_Criterion`.
- `refresh_powerbi.py`
  - legacy `analytics_report_placement`, `analytics_report_criterion`,
    `analytics_report_feed` убраны из refresh list.
- `data_check/verify_big_analytics.py`
  - stale legacy requirements убраны из verifier.
- `tests/test_pbi_contract_lists.py`
  - добавлен regression test, чтобы stale legacy objects не вернулись в builder,
    refresher и verifier contracts.

## Прогон v6

Команда:

```sh
.venv/bin/python3 pipeline.py
```

Run id: `a460eeed0b83`

Сборка данных дошла до star/PBI compatibility. Исходный `pipeline.py` вышел с code 1
только на финальном verifier: verifier еще ожидал удаленные legacy objects. После
синхронизации verifier contract отдельный запуск verifier прошел.

Важные финальные counts:

- `raw_yandex=25,005,168`
- `raw_leads=1,126,130`
- `raw_calls=67,801`
- `big_analytics_full=5,136,442`
- `big_analytics_full_arrival=113,828`
- `big_analytics_unified=5,250,270`
- `fact_big_analytics=5,250,270`
- `bi_pbi_big_analytics_full=5,250,270`
- `bi_fact_direct_feed_funnel=12,801,544`
- `bi_fact_region_spend=13,531,858`
- `bi_fact_adformat_spend=2,974,999`
- `bi_fact_criterion_spend=4,792,311`

Verifier после фикса:

- `PASS`
- `raw_yandex_cost_zero=0`
- `full_before_2026=0`
- `full_null_source=0`
- funnel monotonicity checks all `0`
- `unified_count_mismatch=0`
- `fact_unified_count_mismatch=0`
- golden Kuderko: cost delta `+30.03`, sales `54`, в guard-допуске.

## Состояние v5

Live PostgreSQL `public.data_quality_log` на 2026-08-06:

- fresh `build_unified` прошел для run ids `7db2e48a`, `07f185d3`, `32c4d3f3`;
- все fresh `build_star` упали на `BUILD_STAR_DISK_GUARD`;
- последний успешный v5 `build_star`: `32cf5702`, `2026-08-05 09:57:43`.

Значит сегодняшнее сравнение v5/v6 использует stale v5 star baseline.

## Сравнение v5 vs v6 по main fact

Команда:

```sh
.venv/bin/python3 data_check/compare/run.py
```

Результат: `FAIL`

Период сравнения: `2026-02-01..2026-07-31`, attribution `По дате заявки`.

Provenance:

- v5: `public.fact_big_analytics`, rows `4,153,138`, max Date `2026-07-31`
- v6: `ad_analytics.fact_big_analytics`, rows `4,215,729`, max Date `2026-07-31`, run `a460eeed0b83`

Открытые блокеры:

- расход: `-8,938,616.04` (`-0.77%`)
- обращения: `-5,099.69` (`-1.18%`)
- заявки: `-2,440.51` (`-0.97%`)
- квал: `-2,235.04` (`-4.16%`)
- приедет: `+7` (`+3.50%`)
- приезд: `-545.04` (`-1.37%`)
- продажи: `-56.00` (`-1.57%`)
- не корр: `+1,628` (`+1.15%`)

Сильные очаги из compare report:

- source: `Контекст`, `Посевы_Telegram`, `Пиксель_атрибуц`
- CRM: `Фаиг`, `(пусто)`, `rivendell_excel`
- source table: `direct`, `пиксель_атрибуц`, `calls`, `seo`

## Сравнение всех активных PBI sources

Read-only scan по 34 active CH sources:

- все 34 существуют в v6 ClickHouse;
- 9 являются новыми/split dimensions и отсутствуют в v5 как one-to-one table names:
  `Dim_AdFormat`, `Dim_AdNetworkType`, `Dim_Device`, `Dim_ManagerLogin`, `Dim_PlacementFeed`, `Dim_Source`, `Dim_VkAdGroup`, `Dim_VkAdPlan`, `Dim_VkBanner`;
- comparable objects имеют отличия. Примеры:
  - `Dim_Date`: v5 rows `216`, max `2026-08-04`; v6 rows `217`, max `2026-08-05`;
  - `fact_adformat_spend`: v5 rows `2,811,980`, v6 rows `2,974,999`, cost delta `+154,368,154.44`;
  - `fact_criterion_spend`: v5 rows `4,538,764`, v6 rows `4,792,311`, cost delta `+154,368,100.80`;
  - `fact_region_spend`: v5 rows `12,978,540`, v6 rows `13,531,858`, cost delta `+104,771,637.15`;
  - `fact_criterion_zayavki`: v5 rows `130,982`, v6 rows `128,361`, `kol_vo_zayavok` delta `-8,303`;
  - `fact_region_zayavki`: v5 rows `178,686`, v6 rows `174,621`, `kol_vo_zayavok` delta `-8,818`;
  - `pixel_score`: v5 rows `21,113`, v6 rows `226,166`, `kol_vo_zayavok` delta `-63,285.77`.

## Сравнение raw-layer

Read-only raw checks:

- `raw_yandex`: CH `25,005,168`; PG `public.raw_yandex` `0`.
- `raw_leads`: CH `1,126,130`; PG `public.raw_leads` `976,420`; PG `public.leads` `1,117,456`.
- `raw_calls`: CH `67,801`; PG `public.raw_calls` `73,602`.
- `raw_data.direct_campaigns`: CH `33,738`; PG `public.yandex_direct_history` `89,679`.

Это значит, что минимум часть расхождений начинается до финальных витрин.
Внешний raw loader вне scope этого прохода.

## Power BI report files

Active admin/user PBIP scan после правок:

- нет ссылок на удаленные legacy objects в active admin/user report/model:
  `arp_fact`, `arf_fact`, `arc_fact`, `analytics_report_placement`,
  `analytics_report_criterion`, `analytics_report_feed`, `bi_Dim_Criterion`,
  `Dim_Criterion`.
- missing field references excluding auto date artifacts: `0`.
- stale field refs в active user report перенесены на существующие dimensions:
  `Dim_Campaign.account_login`, `Dim_Adjustment.RlAdjustmentId_total`,
  `Dim_Location.Область`, `Dim_Source.источник`, `Dim_AdFormat.ad_format`.
