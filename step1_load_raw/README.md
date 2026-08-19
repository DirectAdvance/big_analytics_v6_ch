# step1_load_raw — RAW слой ClickHouse

Шаг 1 v6_ch пересоздаёт рабочий RAW-слой в ClickHouse `ad_analytics` из
`raw_data.*` фактов и `reference_data.domains`. Это не PostgreSQL `UNLOGGED`: WAL, `VACUUM`, `SET LOGGED`,
FDW и `local_*`-копии относятся к v5/legacy.

## Назначение

Создаёт/заменяет **5 RAW-объектов** с предвычисленными полями для последующих шагов:

```
raw_data.yandex_direct_report_rows ──► ad_analytics.raw_yandex
raw_data.leads_all + reference_data.domains ─┬► ad_analytics.raw_leads
                                       └► ad_analytics.raw_calls
reference_data.domains ───────────────────────► ad_analytics.raw_domains
нет raw_data.perform_leads ─────────────► ad_analytics.raw_perform_leads (совместимая пустая таблица)
```

`raw_yandex`, `raw_leads` и `raw_calls` пересоздаются как ClickHouse `MergeTree`
таблицы. `raw_domains` — `VIEW` поверх `reference_data.domains`. `raw_perform_leads`
остаётся совместимой пустой таблицей, пока нет живого источника `raw_data.perform_leads`.
Строки `raw_data.leads_all` с `is_copy_for_removal=1` не попадают в CRM RAW-выходы.

## Архитектурная схема

```
raw_data.yandex_direct_report_rows ─────► raw_yandex
                              ├── id, Date, CampaignId, CampaignName, AdGroupId, ...
                              ├── campaign_code = regex(CampaignName)
                              ├── tp / cpc_cpa / site_quiz = SPLIT_PART(campaign_code)
                              ├── adgroup_code = regex(AdGroupName)
                              ├── week_start = DATE_TRUNC('week', Date)
                              └── key3 = Date|CampaignId|AdGroupId|Device|RlAdjustmentId

raw_data.leads_all + reference_data.domains ─► raw_leads (deal_type != 'Звонок', is_copy_for_removal=0)
                              ├── id, created_date, arrival_date, domain_id, domain
                              ├── status, source_type, campaign_id, group_id, correction_id
                              ├── utm_source/medium/campaign/content/term, phone, yclid
                              ├── fid = после 'fid:' в utm_content
                              ├── key3 = ключ по created_date
                              └── key3_arrival_date = ключ по arrival_date

raw_data.leads_all + reference_data.domains ─► raw_calls (deal_type = 'Звонок', is_copy_for_removal=0)
                              └── базовые поля без UTM-ключей (звонки не нужно матчить)

reference_data.domains ─► raw_domains (VIEW)
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

`config/ch_settings.py` и `config/settings.py`:

| Параметр | Значение | Влияет на |
|----------|----------|-----------|
| `EXCLUDED_DOMAIN_NAMES` (`config/ch_settings.py`) | `("victory-crm.ru",)` | Фильтр `raw_leads`/`raw_perform_leads` по ИМЕНИ домена (но НЕ `raw_yandex`) |
| `RAW_SOURCE_TABLES` | `raw_data.*` факты + `reference_data.domains` | `raw_yandex`, `raw_leads`, `raw_calls`, `raw_domains` |
| `RAW_TARGET_TABLES` | `ad_analytics.raw_*` выходы | downstream steps |

`EXCLUDED_DOMAIN_NAMES = ("victory-crm.ru",)` (`step1_load_raw/step1.py::_excluded_domain_names_sql`,
матч по `lowerUTF8(trim(d.domain))` через `LEFT JOIN reference_data.domains`):
- `victory-crm.ru` — тестовый домен, не клиент.
- ⚠️ **Фильтр по ИМЕНИ, не по числовому `domain_id`** — id непереносим между PostgreSQL (v5) и
  ClickHouse (v6), своя нумерация в каждой системе. Раньше здесь буквально копировался v5-список
  `EXCLUDED_DOMAIN_IDS = (1645, 883)`, что в CH исключало не те домены (`multiautos-23.ru`,
  `rt-avtomarket-geely.ru`) и пропускало реальный мусор (`victory-crm.ru`, id=17478 в CH) —
  см. `KNOWN_ISSUES.md` #33.

## Зависимости

- step0 должен пройти ClickHouse preflight.
- Python 3.10+, `clickhouse-connect`, доступ к `victory_clickhouse` через `.secret/loader.py`.

## Примеры запуска

```bash
# Только step1:
.venv/bin/python3 pipeline.py --only-step=1

# Проверка после запуска:
.venv/bin/python3 - <<'PY'
from config.ch_db import get_client
client = get_client()
for table in ["raw_yandex", "raw_leads", "raw_calls", "raw_domains", "raw_perform_leads"]:
    print(table, client.command(f"SELECT count() FROM ad_analytics.{table}"))
PY
```

## Проверки после запуска

```sql
-- Количество строк в каждой RAW
SELECT 'raw_yandex' AS t, count() FROM ad_analytics.raw_yandex
UNION ALL SELECT 'raw_leads', count() FROM ad_analytics.raw_leads
UNION ALL SELECT 'raw_calls', count() FROM ad_analytics.raw_calls
UNION ALL SELECT 'raw_domains', count() FROM ad_analytics.raw_domains;

-- Проверить что campaign_code заполнился (не 'неверный кодер')
SELECT campaign_code, count() FROM ad_analytics.raw_yandex
GROUP BY campaign_code ORDER BY 2 DESC LIMIT 10;

-- Проверить key3 (должен быть LOWER, формат YYYY-MM-DD|...)
SELECT key3 FROM ad_analytics.raw_yandex LIMIT 5;
```

## Связи с другими шагами

- **Зависит от:** step0 (ClickHouse preflight)
- **Используется:** step2 (индексы), step3 (`base_join` CTE, CTE сборки источников), step6 (звонки inline из `raw_calls`)
- **Финализация:** step7 не делает PostgreSQL `UNLOGGED → LOGGED`; v6 RAW уже живёт в ClickHouse.

## Файлы

| Файл | Описание |
|------|----------|
| `step1.py` | Основной скрипт (967 строк) |
| `__init__.py` | Пустой |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |
| `STEP.md` | Сверхкраткая справка по таблицам шага |
