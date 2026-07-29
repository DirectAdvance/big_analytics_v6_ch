# step10_crop_targeting — Посевы (crop targeting)

Шаг 10 пайплайна. Загружает данные о посевах (Telegram/Instagram/VK/MAX/TikTok) из двух источников и доливает в `big_analytics_crop_targeting` + `big_analytics_full`.

## Назначение

Посевы — это нестандартная реклама на каналах в соцсетях, оплачиваемая отдельно. Не идёт через Яндекс.Директ. Источники данных:

| Период | Источник | Таблица результата |
|--------|----------|--------------------|
| До 2026-05-01 | Google Sheets (вручную) | `gsheets_crop_targeting_account_leads` |
| С 2026-05-01 | Telega.in API (автоматически) | `crop_targeting_api_telegain_lead` |

Финальный шаг — `load_crop_to_big_analytics.py` — объединяет оба источника и доливает в `big_analytics_crop_targeting` (`_source_table='crop_targeting'`) и `big_analytics_full`.

## Архитектурная схема

```
Google Sheets ──load_crop_targeting────► gsheets_crop_targeting_account
                                         gsheets_crop_targeting_account_pravilo_utm
                          │
                          ▼
local_leads_all ──load_crop_targeting_leads──► gsheets_crop_targeting_account_leads
                                                       (до 2026-05-01)

Telega.in API ──fetch_api──► crop_targeting_api_telegain (исторические)
                                  │
local_telega_in_orders (FDW) ─────┤
                                  ▼
                       load_api_leads (load_telega_in_orders)
                                  │
                                  ▼
                       crop_targeting_api_telegain_lead
                                  (с 2026-05-01, с трансформациями)
                                  │
                                  ▼
                       load_crop_to_big_analytics
                                  │
                                  ├──► big_analytics_crop_targeting (_source_table='crop_targeting')
                                  └──► big_analytics_full (_source_table='crop_targeting')
```

## Трансформации load_api_leads

`crop_targeting_api_telegain_lead`:

| Поле | Источник | Трансформация |
|------|----------|----------------|
| `"Date"` | `utm_content` (DDMMYYYY) | `TO_DATE(SUBSTR(utm_content,1,8), 'DDMMYYYY')` |
| `total_cost` | `user_price` (= `price` после миграции) | `× 1.22 × 1.30` (НДС + наценка агентства) |
| `"CampaignName"` | `channel_link` | как есть |
| `domain` | `post_links` jsonb | `post_links → jsonb_array_elements ->> 0` |
| `источник` | `channel_link` regex | URL pattern → telegram/instagram/VK/TikTok |
| `салон`, `город`, `регион`, `специалист`, `тип_сайта`, `шаблон`, `статус`, `direction` | `local_gsheet_sites` по `domain` | lookup |

Фильтр: только записи где `utm_content` = 8 цифр (DDMMYYYY).

Матчинг лидов из `local_leads_all`: 5 полей — домен + `utm_campaign` + `utm_content` (lpad 8) + `utm_source` + `utm_medium`.

## Стоимость

Активный путь (`load_telega_in_orders.py`): `total_cost = total_price` (берётся напрямую из заказа).

Исторический путь (`load_api_leads.py`, не в pipeline):
```
total_cost = price × 1.22 × 1.30
```
- `× 1.22` — НДС 22%; `× 1.30` — наценка агентства 30%

## load_crop_to_big_analytics.py

```python
1. DELETE FROM big_analytics_crop_targeting WHERE _source_table='crop_targeting'
2. INSERT FROM gsheets_crop_targeting_account_leads → big_analytics_crop_targeting  (Date < '2026-05-01')
3. INSERT FROM crop_targeting_api_telegain_lead    → big_analytics_crop_targeting  (Date >= '2026-05-01')
4. DELETE FROM big_analytics_full WHERE _source_table='crop_targeting'
5. INSERT FROM big_analytics_crop_targeting WHERE _source_table='crop_targeting' → big_analytics_full
6. UPDATE big_analytics_full SET "Название crm" = src.crm_name FROM (... GROUP BY салон ...) src
   WHERE f."Название crm" IS NULL  -- backfill по салону
```

## Параметры

- Spreadsheet ID: `1RgYaXiCgiipV1ljWFsiVYVzDQJZv1-V1w9hFdegQ0lI`
- Лист: `Лист1`
- Service account: `cedar-gearbox-464117-e5-676d6cc8937e.json`
- Telega.in токен: `.secret/.env` через `loader.py` (`load_telega()`)

## Зависимости

- step0 (`local_leads_all`, `local_gsheet_sites`, `local_telega_in_orders` FDW)
- step3 (`big_analytics_crop_targeting` DDL)
- step6 + step7 (`big_analytics_full` финализирован)
- Google API + service account
- Telega.in API + токен

## Примеры запуска

```bash
# Полный pipeline посевов (4 шага):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step10_crop_targeting/pipeline.py"

# Только step10 в составе общего пайплайна:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=10"

# Отдельные подшаги:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step10_crop_targeting/load_crop_targeting.py"
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step10_crop_targeting/load_crop_targeting_leads.py"
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step10_crop_targeting/load_telega_in_orders.py"
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step10_crop_targeting/load_crop_to_big_analytics.py"
```

## Проверки после запуска

```sql
-- Источники в big_analytics_crop_targeting (все 6 _source_table)
SELECT _source_table, COUNT(*), SUM(total_cost), MIN("Date"), MAX("Date")
FROM big_analytics_crop_targeting GROUP BY _source_table ORDER BY 2 DESC;

-- В big_analytics_full только _source_table='crop_targeting' от step10
SELECT _source_table, COUNT(*), SUM(total_cost)
FROM big_analytics_full
WHERE _source_table = 'crop_targeting'
GROUP BY 1;

-- Май+ через API, не gsheets
SELECT DATE_TRUNC('month', "Date"), COUNT(*), SUM(total_cost)
FROM big_analytics_crop_targeting
WHERE _source_table = 'crop_targeting'
GROUP BY 1 ORDER BY 1;
```

## История фиксов

| Дата | Фикс |
|------|------|
| Апрель 2026 | DELETE сужен до `_source_table='crop_targeting'` (не сносит tp8/calls/seo посевных доменов) |
| Апрель 2026 | `_move_tp8_to_crop` ставит `'tp8'`, не `'crop_targeting'` |
| 18.05.2026 | Миграция `crop_targeting_api_telegain` → raw `telega_in_orders` (через FDW) |
| 2026-05-20 | Фикс задвоения Max/VK в мае (NOT EXISTS в step3, не в step10) |

## Связи

- **Зависит от:** step0, step3, step6, step7
- **Включена в:** `pipeline.py` после step9
- **Включена в `fast_pipeline.py`** — напрямую через `load_telega_in_orders.py` + `load_crop_to_big_analytics.py` (FDW, без прямого API-вызова)
- **Связана с step3**: 6 типов `_source_table` в `big_analytics_crop_targeting`, только `'crop_targeting'` через step10

## Файлы

| Файл | Описание |
|------|----------|
| `step10.py` | Точка входа (запускает `load_telega_in_orders` + `load_crop_to_big_analytics`) |
| `pipeline.py` | Полный pipeline посевов (4 подшага) |
| `load_crop_targeting.py` | Google Sheets sync |
| `load_crop_targeting_leads.py` | gsheets + лиды (до мая) |
| `load_telega_in_orders.py` | **Активный**: FDW `local_telega_in_orders` → `crop_targeting_api_telegain_lead` |
| `load_api_leads.py` | Исторический (не вызывается в pipeline): `crop_targeting_api_telegain` → `crop_targeting_api_telegain_lead` |
| `fetch_api.py` | Telega.in API → `crop_targeting_api_telegain` (используется standalone / исторически) |
| `load_crop_to_big_analytics.py` | Финальный INSERT в big_analytics_* |
| `SPEC_fetch_api_telegain.md` | Спецификация fetch_api.py (Telega.in API) |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |
