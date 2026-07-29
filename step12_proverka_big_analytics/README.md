# step12_proverka_big_analytics — Сверка bigA с CSD по грани CRM

Шаг 12 пайплайна. Сверяет расход из `fact_big_analytics` (BI-сторона, `ad_analytics_bi`)
против `campaign_stats_daily` (CSD-сторона, `ad_analytics`) по грани CRM.
Только домены с `niche='Авто'` из `local_gsheet_sites`. Результат — таблица
`analytics_proverka_big_analytics` + Telegram-сообщение со списком расхождений.

## Назначение

После шагов 1-7 у нас собрана `fact_big_analytics` со своей версией воронки.
Источник `ad_analytics` содержит `campaign_stats_daily` (расходы CSD).
step12 сверяет расход по CRM × месяц с 2026-01-01.
CRM-маппинг: dominant «Название crm» per domain из `fact_big_analytics` (1:1, по spend).

## Архитектурная схема

```
ad_analytics_bi.local_gsheet_sites ──────────► auto-domains (niche='Авто')
ad_analytics_bi.fact_big_analytics ─┬────────► CRM-map (dominant crm per domain, 1:1)
                                     └────────► BI spend+funnel per (CRM, month)
                                                      │
ad_analytics.campaign_stats_daily ──► CSD (dedup) ───┤
(MAX per (cid,day) → SUM)                             │
                                          diff_spend = csd - bi
                                                      │
                                  analytics_proverka_big_analytics (DROP+INSERT)
                                                      │
                                     Telegram (расход per CRM + trust-индикатор)
```

## Метрики сравнения

| BI (`fact_big_analytics`)        | CSD (`campaign_stats_daily`) |
|----------------------------------|------------------------------|
| `bi_spend` = direct+tp8+tp9+tp10 | `csd_spend` (dedup MAX/day)  |

BI воронка (`bi_korr`, `bi_visits`, `bi_sales`, `bi_leads`) — справочно, без CSD-аналога.

## CRM-map и периметр

- CRM-map: `DISTINCT ON (dom)` из `fact_big_analytics WHERE _source_table='direct'`,
  ranked by `SUM(total_cost) DESC` — 1:1 dom→crm, нет задвоений.
- Периметр: только авто-домены из `local_gsheet_sites WHERE niche='Авто'` (lower/trim).
- Несматченные cid → `__non_auto_residual__` (справочная строка, в Δ не входит).

## Колонки `analytics_proverka_big_analytics`

| Колонка           | Тип         | Описание                                   |
|-------------------|-------------|--------------------------------------------|
| crm_name          | TEXT        | «Название crm» или `__non_auto_residual__` |
| month             | DATE        | начало месяца                              |
| csd_spend         | NUMERIC     | расход CSD (dedup MAX per day → SUM)       |
| bi_spend          | NUMERIC     | BI: direct+tp8+tp9+tp10 итого              |
| bi_direct_spend   | NUMERIC     | BI: только direct                          |
| bi_tp8_spend      | NUMERIC     | BI: tp8/tp9/tp10                           |
| bi_leads          | BIGINT      | BI обращения direct+tp8                   |
| bi_direct_leads   | BIGINT      | BI обращения direct                       |
| bi_tp8_leads      | BIGINT      | BI обращения tp8/tp9/tp10                 |
| bi_korr           | BIGINT      | BI корректные (direct)                     |
| bi_visits         | BIGINT      | BI визиты (direct)                         |
| bi_sales          | BIGINT      | BI продажи (direct)                        |
| diff_spend        | NUMERIC     | csd_spend − bi_spend                       |
| bi_domain_count   | INTEGER     | число BI-доменов CRM                       |
| generated_at      | TIMESTAMPTZ | время выполнения                           |

PRIMARY KEY: `(crm_name, month)`.

## BI vs CSD

- **BI** = `fact_big_analytics WHERE _source_table IN ('direct','tp8','tp9','tp10')` по авто-доменам CRM;
  direct и tp8/tp9/tp10 аккумулируются раздельно.
- **CSD** = `MAX(total_spend) per (campaign_id, day)` → SUM (v7 дедуп дублей `campaign_stats_daily`);
  cid маппится cid→dominant_domain→crm.

## Cross-DB соединения

| Соединение       | БД                | Назначение                                               |
|------------------|-------------------|----------------------------------------------------------|
| `conn` (главный) | `ad_analytics_bi` | CRM-map, BI select, INSERT в `analytics_proverka_big_analytics` |
| `DB_SRC`         | `ad_analytics`    | read-only для CSD (`campaign_stats_daily`)               |

## Параметры

```python
DATE_START = date(2026, 1, 1)
SPEND_THRESHOLD_PCT = 5.0     # порог |Δ%| расхода для вердикта ⚠️
LEADS_THRESHOLD_PCT = 10.0    # порог |Δ%| заявок для вердикта ⚠️
NON_AUTO_LABEL = '__non_auto_residual__'
```

## Зависимости

- step3 / step6 / step7 (`fact_big_analytics` актуален)
- Доступ к `ad_analytics` (read-only)

## Примеры запуска

```bash
# В составе pipeline:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py"

# Только step12:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 -m step12_proverka_big_analytics.step12"

# Просмотр результата (через pgq.py, psql на Victory сломан):
ssh victory "~/venv/bin/python3 ~/pgq.py \"
SELECT crm_name, month, csd_spend, bi_spend, diff_spend
FROM analytics_proverka_big_analytics
WHERE ABS(diff_spend) > 1000
ORDER BY ABS(diff_spend) DESC LIMIT 30;
\""
```

## Проверки после запуска

```sql
-- Объём отчёта
SELECT COUNT(*), MIN(month), MAX(month) FROM analytics_proverka_big_analytics;

-- Большие расхождения по расходам
SELECT crm_name, month, csd_spend, bi_spend, diff_spend
FROM analytics_proverka_big_analytics
WHERE ABS(diff_spend) > 5000
ORDER BY ABS(diff_spend) DESC;

-- Разбивка BI direct vs tp8 по CRM
SELECT crm_name, month, bi_direct_spend, bi_tp8_spend, bi_spend
FROM analytics_proverka_big_analytics
WHERE crm_name != '__non_auto_residual__'
ORDER BY crm_name, month;
```

## Telegram

После INSERT отправляет в Telegram отчёт с расхождениями (где `|Δ%| > 5%`).
Включает trust-индикатор: (cid,day)-дубли CSD и концентрацию Δ в top-1 cid per CRM.
Сообщение режется на чанки по 4000 символов.

## Связи

- **Зависит от:** step3, step6, step7
- **После step12**: `crm_mappings_check` (отдельная проверка маппингов CRM-статусов), затем step8

## Файлы

| Файл | Описание |
|------|----------|
| `step12.py` | Основной скрипт (cross-DB сверка + Telegram) |
| `__init__.py` | Пустой |
| `MEMORY.md` | Память агента по этому шагу |
| `CLAUDE.md` | Детальная инструкция |
| `README.md` | Этот файл |
