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
`../PBI_TABLES.md` §0 (PBI-паритет). `Пиксель_атрибуц` больше не является разрешённой осью:
в BA5 и BA6 должен оставаться обычный `Пиксель`, без отдельной attribution-копии.

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
- `compare/run.py` должен доходить до числовой сверки. Текущее падение на контракте схемы —
  отдельный OPEN-дефект, а не результат сравнения v5 и v6.
