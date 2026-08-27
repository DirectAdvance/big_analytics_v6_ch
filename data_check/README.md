# data_check — проверки данных v6_ch

`data_check` содержит два разных контура проверок:

1. `verify_big_analytics.py` — post-run verification внутри `pipeline.py`, шаг 900.
2. `compare/` — гейт сверки v5↔v6 перед переходом потребителей на v6.

## Запуск post-run verify

Обычно запускается самим пайплайном:

```bash
.venv/bin/python3 pipeline.py
```

Отдельный запуск:

```bash
.venv/bin/python3 -m data_check.verify_big_analytics
```

Проверяет ClickHouse-объекты `ad_analytics.*` и `bi_*`: наличие ключевых таблиц/вьюх, row-counts,
инварианты воронки, golden Кудерко, совместимость `fact_big_analytics` и `big_analytics_unified`.
BI golden-контракты блокируют шаг 900 и Power BI refresh, если:

- расход Директа по авто в `raw_yandex` не дошёл до рабочего слоя или `pbi_big_analytics_full`
  в срезе `месяц + login + domain` с допуском 1 ₽;
- расход фидов из `raw_data.direct_feed_report_rows` не дошёл до `fact_direct_feed_funnel`
  в срезе `месяц + login + domain` с допуском 1 ₽;
- воронка `big_analytics_full` / `big_analytics_full_arrival` не равна `pbi_big_analytics_full`
  по двум осям атрибуции;
- закрытые месяцы изменили продажи или CPL продажи больше чем на 4% к прошлому успешному
  снимку `pipeline_run_snapshot_v`;
- авто-метрики в BI попали в пустой город/салон или к служебным специалистам
  `Без специалиста`, `Посевы`, `Тоборев Владимир`.

Инвариант воронки включает кредитные шаги: `korr >= kval >= priezd >= dohod_do_kredita >= dobro >= prodazhi`.
Для PBI-источников отдельно проверяются `fact_region_zayavki`, `fact_criterion_zayavki`,
`bi_fact_direct_feed_funnel` и `fact_ml_korrektirovki`. Лёгкая
`fact_direct_feed_funnel` не содержит кредитные шаги и не подходит для этого инварианта.

Важно: golden Кудерко в v6 зависит от внешнего бэкфила `raw_data.yandex_direct_report_rows`.
Если логируется `KUDERKO_RAW_INCOMPLETE`, это означает неполное сырьё по 67 логинам Кудерко
и относится к known issue #37.

## Запуск v5↔v6 compare

```bash
.venv/bin/python3 -m data_check.compare.run
```

Назначение: сравнить production v5 и v6_ch на общем периоде и локализовать расхождения по
месяцу, source/source_table, специалисту и CRM.

Статус на 2026-08-20: compare-контракт читает star/light fact v6 и восстанавливает
`специалист` / `Название crm` через `Dim_Salon` / `Dim_CRMStatus` с fallback на `Dim_Site`.
Последний live-запуск после `run_id=ed6bfc6f9c23` дошёл до числовой сверки и вернул `FAIL` по
реальным принятым дельтам v5↔v6, а не из-за схемы.
Подробный PBI-паритет — `../PBI_TABLES.md` §0. `Пиксель_атрибуц` больше не является разрешённой
осью: в BA5 и BA6 должен оставаться обычный `Пиксель`, без отдельной attribution-копии.

**Whitelist пустоты в `verify_big_analytics.py` убран 2026-08-17.** Непустота PBI-объектов
проверяется (`empty_pbi_view:`), а `PBI_EMPTY_ALLOWED` теперь пустой. Все активные `bi_*`-витрины
должны быть непустыми.

`PBI_EMPTY_BY_DESIGN` тоже пустой. Если свежая среда ещё не прогнала night step14 и
`bi_*minus*` пустые, это теперь честный FAIL, а не скрытый пропуск. Инвариант закреплён тестом
`test_pbi_empty_whitelist_is_empty`. Return commission выведен из активного BA6 PBI-контракта и
удалён из live ClickHouse 2026-08-17.

## Архитектура

```text
verify_big_analytics.py
  └─ master verification для ClickHouse v6_ch

compare/run.py
  ├─ contract.py  — проверка схемы входных таблиц
  ├─ sources.py   — чтение v5/v6 и приведение к общему формату
  ├─ differ.py    — числовая сверка и локализация дельт
  └─ report.py    — текстовый отчёт гейта
```

## Что считать успешным

- `pipeline.py` должен завершаться строкой `big_analytics_v6_ch pipeline OK`.
- `verify_big_analytics` должен возвращать `PASS`; предупреждение `KUDERKO_RAW_INCOMPLETE`
  допустимо только пока открыт known issue #37.
- `compare/run.py` должен доходить до числовой сверки. `FAIL` сейчас означает реальные дельты
  BA5↔BA6, а не падение контракта схемы.
