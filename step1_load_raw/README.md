# step1_load_raw — Загрузка RAW UNLOGGED таблиц

Второй шаг пайплайна. Перекладывает данные из локальных копий (`local_*`) в RAW-таблицы `UNLOGGED`. UNLOGGED = без записи в WAL — INSERT работает в 2–3 раза быстрее, что важно при перегрузке миллионных датасетов на каждый прогон.

## Назначение

Создаёт **4 RAW-таблицы** с предвычисленными полями для последующих шагов:

```
yandex_direct_manager_reports (FDW) ──► raw_yandex     (расходы + campaign_code/tp/cpc_cpa/site_quiz/adgroup_code/key3)
local_leads_all ─┬► raw_leads          (не-звонки, без excluded domains + fid + key3 + key3_arrival_date)
                 └► raw_calls          (только звонки)
local_domains ──► raw_domains          (без изменений)
local_perform_leads ──► raw_perform_leads (perform-заявки + дедуп кросс-доменных продаж + ветка (b) из local_leads_all)
```

Каждая RAW-таблица создаётся через `DROP TABLE IF EXISTS` + `CREATE UNLOGGED TABLE AS SELECT` — всегда чистый срез данных.

## Архитектурная схема

```
local_yandex (LOGGED) ─────► raw_yandex   (UNLOGGED)
                              ├── id, Date, CampaignId, CampaignName, AdGroupId, ...
                              ├── campaign_code = regex(CampaignName)
                              ├── tp / cpc_cpa / site_quiz = SPLIT_PART(campaign_code)
                              ├── adgroup_code = regex(AdGroupName)
                              ├── week_start = DATE_TRUNC('week', Date)
                              └── key3 = Date|CampaignId|AdGroupId|Device|RlAdjustmentId

local_leads_all + local_domains ─► raw_leads (UNLOGGED, deal_type != 'Звонок')
                              ├── id, created_date, arrival_date, domain_id, domain
                              ├── status, source_type, campaign_id, group_id, correction_id
                              ├── utm_source/medium/campaign/content/term, phone, yclid
                              ├── fid = после 'fid:' в utm_content
                              ├── key3 = ключ по created_date
                              └── key3_arrival_date = ключ по arrival_date

local_leads_all + local_domains ─► raw_calls (UNLOGGED, deal_type = 'Звонок')
                              └── базовые поля без UTM-ключей (звонки не нужно матчить)

local_domains ─► raw_domains (UNLOGGED, копия)
```

## Ключевая логика — `key3`

`key3` — композитный ключ для матчинга **лид ↔ расход Директа**.

Формула: `LOWER(Date|CampaignId|AdGroupId|Device|RlAdjustmentId)`

Особенности:
- `Date` форматируется как `YYYY-MM-DD`
- **`AdGroupId='0'` для tp6/tp7** (МК/ТК кампании без групп в Директе)
- `Device` нормализуется в `mobile/desktop/tablet/smart_tv/0`
- В `raw_leads` `Device` извлекается из `utm_content LIKE '%dev:mobile%'`

## Нормализация кириллических lookalike-символов

В названиях кампаний встречаются **кириллические символы**, визуально идентичные латинским. Без нормализации `REGEXP_MATCH` не сработает → `campaign_code='неверный кодер'`.

```sql
REPLACE("CampaignName", chr(1089), 'c')
-- chr(1089) = 'с' (кирил., U+0441) → 'c' (лат.)
```

Применяется в каждом из 3-х `REGEXP_MATCH` для `campaign_code`, `tp/cpc_cpa/site_quiz`.

## Параметры

`config/settings.py`:

| Параметр | Значение | Влияет на |
|----------|----------|-----------|
| `EXCLUDED_DOMAIN_IDS` | `(1645, 883)` | Фильтр `raw_leads` (но НЕ `raw_yandex`) |
| `T_RAW_YANDEX`, `T_RAW_LEADS`, `T_RAW_CALLS`, `T_RAW_DOMAINS` | имена таблиц | |
| `T_YANDEX_LOCAL`, `T_LEADS_ALL_LOCAL`, `T_DOMAINS_LOCAL` | имена локальных копий | |

`EXCLUDED_DOMAIN_IDS = (1645, 883)`:
- `1645` — priezd shared key3 (общий ключ, искажает статистику)
- `883` — `victory-crm.ru` (не клиент)

## Зависимости

- step0 должен быть выполнен (нужны `local_*` таблицы)
- Python 3.10+, psycopg2

## Примеры запуска

```bash
# Только step1:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=1"

# Проверка после запуска:
psql -c "SELECT relname, pg_size_pretty(pg_relation_size(relname::regclass))
         FROM (VALUES ('raw_yandex'),('raw_leads'),('raw_calls'),('raw_domains')) AS t(relname);"
```

## Проверки после запуска

```sql
-- Количество строк в каждой RAW
SELECT 'raw_yandex' AS t, COUNT(*) FROM raw_yandex
UNION ALL SELECT 'raw_leads',   COUNT(*) FROM raw_leads
UNION ALL SELECT 'raw_calls',   COUNT(*) FROM raw_calls
UNION ALL SELECT 'raw_domains', COUNT(*) FROM raw_domains;

-- Проверить что campaign_code заполнился (не 'неверный кодер')
SELECT campaign_code, COUNT(*) FROM raw_yandex
GROUP BY campaign_code ORDER BY 2 DESC LIMIT 10;

-- Проверить key3 (должен быть LOWER, формат YYYY-MM-DD|...)
SELECT key3 FROM raw_yandex LIMIT 5;
```

## Связи с другими шагами

- **Зависит от:** step0 (`local_*`)
- **Используется:** step2 (индексы), step3 (`base_join` CTE, CTE сборки источников), step6 (звонки inline из `raw_calls`)
- **UNLOGGED → LOGGED:** конвертация в step7

## Файлы

| Файл | Описание |
|------|----------|
| `step1.py` | Основной скрипт (967 строк) |
| `__init__.py` | Пустой |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |
| `STEP.md` | Сверхкраткая справка по таблицам шага |
