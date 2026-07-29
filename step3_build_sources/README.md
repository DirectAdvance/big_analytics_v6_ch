# step3_build_sources — Сборка источниковых таблиц

Четвёртый и самый объёмный шаг пайплайна. Собирает **5 источниковых таблиц** из RAW + локальных gsheets + правил из `local_crm_statuses`. После этого шага все компоненты воронки готовы, остаётся только объединить их в `big_analytics_full` (step6).

## Назначение

| Таблица | Содержимое | Объём |
|---------|-----------|-------|
| `big_analytics_direct` | Расходы Директа + лиды с UTM Директа | ~700k+ строк |
| `big_analytics_seo` | SEO-лиды без UTM или с seo-UTM | ~10k+ строк |
| `big_analytics_pixel` | DDL пустой, заполняется step5 | 0 |
| `big_analytics_crop_targeting` | Посевы (9 источников `_source_table`, включая VK Ads) | ~16k+ строк |
| `big_analytics_reviews` | Кампании отзывов | ~3k+ строк |

## Архитектурная схема

```
raw_yandex + raw_leads + raw_domains + gsheets ──► big_analytics_direct
                            │
                            ├─► _move_tp8_to_crop ──► big_analytics_crop_targeting (_source_table='tp8')
                            └─► _patch_fid_attribution

raw_leads (UTM пустые / seo-UTM)                 ──► big_analytics_seo

gsheets_crop_targeting_account (Google Sheets)   ──► big_analytics_crop_targeting (_source_table='crop_targeting')
                                                     │
raw_calls (19 посевных доменов) ─────────────────────┤ (_source_table='calls')
raw_leads (19 посевных доменов, SEO)─────────────────┤ (_source_table='seo')
raw_leads (telegram_storis) ─────────────────────────┤ (_source_table='telegram')
raw_leads (Max/VK социальные посевы) ────────────────┤ (_source_table='social_посевы')
crop_targeting_api_telegain_lead (Telega.in API) ────┘ (через step10)
local_vk_ads_stats_day + raw_leads (VK Ads, direction='Авто') ───┤ (_source_table='vk_ads'/'vk_zero'/'vk_perform')

yandex_direct_reports_reviews ─────► big_analytics_reviews
```

## Источники посевов в `big_analytics_crop_targeting`

| `_source_table` | Откуда | Объём (на 2026-05-20) |
|----------------|--------|----------------------|
| `tp8` | МК/ТК кампании Директа (через `_move_tp8_to_crop`) | ~14 341 |
| `crop_targeting` | Google Sheets + Telega.in API (через step10) | ~994 |
| `calls` | Звонки 19 посевных доменов | ~538 |
| `seo` | SEO-лиды 19 посевных доменов | ~269 |
| `social_посевы` | raw_leads с UTM Max/VK | ~89 |
| `telegram` | raw_leads с UTM telegram_storis | ~56 |
| `vk_ads` | Расход VK Реклама (`local_vk_ads_stats_day`, `_add_vk_ads_to_crop_sql`, VK_ADS_INTEGRATION_2026-07-06) | — |
| `vk_zero` | Лиды на VK-доменах без числового `utm_campaign` (VK_ADS_ZERO_LEADS_2026-07-07) | — |
| `vk_perform` | Потерянные vkads-заявки Перформа (VK_PERFORM_LEADS_2026-07-10) | — |

## Общие CTE (используются во всех `_build_*_sql`)

| CTE | Назначение |
|-----|-----------|
| `plan_fakt_cte` | План/факт по (салон, тип) |
| `account_manager_map` | manager_login по account_login (для строк без менеджера в Яндексе) |
| `leads_deduped` | Дедупликация по (phone, yclid), приоритет visit/sale статуса |
| `domain_source_type` | Тип CRM по домену через приоритеты (Маркар > Мега > Фаиг > Плекс > прочие) |

## Воронка через `local_crm_statuses`

`config/status_sql.py` собирает воронку через 2 стороны:

**Status-сторона** (берёт `leads.status`, `kind='status'`):
```
обращения ⊇ заявки ⊇ квал ⊇ визит ⊇ продажа
kol_vo_zayavok ⊇ korr ⊇ kval ⊇ priezd ⊇ prodazhi
```

**Reason-сторона** (берёт `leads.reason`, `kind='reason'`):
```
доход ⊇ добро
dohod_do_kredita ⊇ dobro
```

Auto-merge добавляет переходы автоматически: `sale → visit → qualified → correct` (status), `sale → approved → credit` (reason).

Хардкод-метрики (не из таблицы): `ne_otvechaet`, `filtr`, `nedozvon`, `priedet`.

## Параметры

`config/settings.py`:

| Параметр | Значение |
|----------|----------|
| `WORK_MEM` | `'1999MB'` — `SET work_mem` перед массивными CTE (session-level, не `SET LOCAL`); `big_analytics_direct` CTAS отдельно получает `4096MB` serial (`STEP3_DIRECT_WORKMEM_2026-07-11`) |
| `T_DIRECT/SEO/PIXEL/CROP/REVIEWS` | Имена результирующих таблиц |
| `T_LEADS_CROP_ATTR` | `'crop_targeting_api_telegain_lead'` |

## Зависимости

- step1 (RAW-таблицы) + step2 (индексы)
- `local_gsheet_*` из step0
- `local_crm_statuses` из step0
- `crop_targeting_api_telegain_lead` (если посевы за май+) — заполняется отдельным циклом `step10/load_api_leads.py`

## Примеры запуска

```bash
# Только step3:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=3"

# step3 + corrections (рекомендуется вместе, т.к. corrections использует результаты step3):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 -c 'from step3_build_sources import step3; import corrections; from config.db import get_conn; c=get_conn(); step3.run(c, \"manual\"); corrections.apply(c)'"
```

## Проверки после запуска

```sql
-- Объёмы 5 таблиц
SELECT 'direct' AS t, COUNT(*) FROM big_analytics_direct
UNION ALL SELECT 'seo',    COUNT(*) FROM big_analytics_seo
UNION ALL SELECT 'pixel',  COUNT(*) FROM big_analytics_pixel
UNION ALL SELECT 'crop',   COUNT(*) FROM big_analytics_crop_targeting
UNION ALL SELECT 'reviews', COUNT(*) FROM big_analytics_reviews;

-- Источники в crop_targeting
SELECT _source_table, COUNT(*), SUM(total_cost)
FROM big_analytics_crop_targeting GROUP BY _source_table ORDER BY 2 DESC;

-- tp8 строки должны быть в crop, не в direct
SELECT _source_table FROM big_analytics_crop_targeting WHERE tp='tp8' LIMIT 1;
-- Ожидаем: 'tp8' (НЕ 'crop_targeting'!)
```

## История фиксов

| Дата | Фикс |
|------|------|
| Апрель 2026 | `_move_tp8_to_crop` теперь ставит `_source_table='tp8'`, не `'crop_targeting'` (иначе load_crop удалял 14k строк) |
| Май 2026 | Оптимизация `_recompute_ag_parts` через UNLOGGED lookup: 188с → 63с |
| Май 2026 | `domain_source_type` через приоритет ARRAY_AGG, не MAX (Unicode-баг Маркар→Плекс) |
| Май 2026 | `_add_social_posev_to_crop_sql` + NOT EXISTS к Telega.in для май+ (задвоение Max/VK) |

## Связи

- **После step3:** `corrections.apply()` (правила специалистов, пересчёт ag_parts, normalize_salons)
- **Используется:** step4 (`big_analytics_direct.campaign_status`), step6 (UNION ALL → full), step10 (через `crop_targeting`)

## Файлы

| Файл | Описание |
|------|----------|
| `step3.py` | Основной модуль (~3000+ строк генерации SQL) |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |
| `STEP.md` | Краткий конспект шага (таблицы/части/work_mem) |
