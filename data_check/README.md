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

Важно: golden Кудерко в v6 зависит от внешнего бэкфила `raw_data.yandex_direct_report_rows`.
Если логируется `KUDERKO_RAW_INCOMPLETE`, это означает неполное сырьё по 67 логинам Кудерко
и относится к known issue #37.

## Запуск v5↔v6 compare

```bash
.venv/bin/python3 -m data_check.compare.run
```

Назначение: сравнить production v5 и v6_ch на общем периоде и локализовать расхождения по
месяцу, source/source_table, специалисту и CRM.

Текущий статус на 2026-08-10: compare-контракт требует доработки под star/light fact v6.
`fact_big_analytics` больше не содержит wide-колонки `специалист` и `"Название crm"`; они
восстанавливаются через dimensions. До исправления `compare/run.py` падает с `exit 2`:

```text
контракт разошёлся со схемой v6 -> ad_analytics.fact_big_analytics:
нет колонок специалист, Название crm
```

См. `KNOWN_ISSUES.md` #38.

⚠️ **Статус на 2026-08-15 — по-прежнему `exit 2`, гейтом пользоваться нельзя.** Ручная сверка
за 2026-08-15 сделана в обход и записана в `../RAW_DIFF_FINDINGS.md` (сырьё) и
`../PBI_TABLES.md` §0 (PBI-паритет). Дополнительно: `data_check/compare/contract.json` штатно
покраснеет по строке `Пиксель_атрибуц` — в v5 она есть на заявочной оси, в v6 с 2026-08-15
только на визитной. Это ожидаемо, не дефект.

⚠️ **Whitelist пустоты в `verify_big_analytics.py` шире, чем нужно.** Непустота PBI-объектов
проверяется (`empty_pbi_view:`), но `PBI_EMPTY_ALLOWED` разрешает пустоту 12 именам, из которых
реально пусты только две заглушки (`bi_check_utm_fuck_direct`,
`bi_yandex_direct_return_commission_report`). Остальные десять — живые витрины, и их обнуление
прошло бы гейт молча.

С 2026-08-16 пустой whitelisted-объект логируется как `WARNING PBI_VIEW_EMPTY_WHITELISTED`
(две заглушки вынесены в `PBI_EMPTY_BY_DESIGN` и молчат штатно). Вердикт не изменился.
Сужение самого whitelist до двух заглушек — `KNOWN_ISSUES.md` #40, ждёт решения Семёна:
это превращает пустоту в FAIL прод-прогона, а `bi_*minus*` зависят от выключенного step14.

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
- `compare/run.py` должен доходить до числовой сверки. Текущее падение на контракте схемы —
  отдельный OPEN-дефект, а не результат сравнения v5 и v6.
