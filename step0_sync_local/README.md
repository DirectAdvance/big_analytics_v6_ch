# step0_sync_local — Синхронизация локальных копий

Первый шаг пайплайна. Копирует данные из БД-источника `ad_analytics` в рабочую БД `ad_analytics_bi`. Это разделение даёт изоляцию: остальные шаги читают только локальные `local_*` копии и не нагружают источник.

## Назначение

| Источник (`ad_analytics`) | Локальная копия (`ad_analytics_bi`) | Режим |
|---------------------------|--------------------------------------|-------|
| `yandex_direct_manager_reports` | `local_yandex` | **НЕ синкается** (VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17; потребители читают FDW напрямую) |
| `leads_all` | `local_leads_all` | TRUNCATE+INSERT (окно: `created_date >= DATE_FROM OR arrival_date >= DATE_FROM`) |
| `perform_leads` | `local_perform_leads` | TRUNCATE+INSERT (то же окно; PERFORM_LEADS_2026-07-01) |
| `domains` | `local_domains` | TRUNCATE+INSERT |
| `gsheet_sites` | `local_gsheet_sites` | TRUNCATE+INSERT |
| `gsheet_naming` | `local_gsheet_naming` | TRUNCATE+INSERT |
| `gsheet_plan_fakt` | `local_gsheet_plan_fakt` | TRUNCATE+INSERT |
| `gsheet_autosalony_clients` | `local_gsheet_autosalony_clients` | TRUNCATE+INSERT |
| `gsheet_priezdi_marcar` | `local_gsheet_priezdi_marcar` | TRUNCATE+INSERT |
| `crm_statuses` | `local_crm_statuses` | TRUNCATE+INSERT (с маппингом колонок `value→crm_status`) |
| `telega_in_orders` (FDW в `ad_analytics_bi`) | `local_telega_in_orders` | TRUNCATE+INSERT из FDW + замены UTM из JSON; защита от 0-строк |
| `vk_ads_stats_day` (FDW в `ad_analytics_bi`) | `local_vk_ads_stats_day` | TRUNCATE+INSERT, banner-grain, только Авто-аккаунты spent>0 (VK_ADS_INTEGRATION_2026-07-06) |

После step0 запускаются **patches**:

1. `_patch_remove_elit_avto()` — удаляет строки закрытого 'Элит Авто' из `local_gsheet_sites` (fan-out расхода).
2. `_insert_pixel_pr_sites()` — добавляет 7 pixel_pr доменов Перформа в `local_gsheet_sites` (PIXEL_PR_2026-07-09).
3. `_patch_marcar_statuses()` — backfill статусов продаж Маркар из Google Sheets «Маркар Доезды» (CRM не синхронизирует продажи обратно).
4. `_ensure_marcar_crm_statuses()` — добавляет недостающие маппинги Маркар в `local_crm_statuses`.
5. `_ensure_crmf_lider_crm_statuses()` — salon-specific маппинги crmf+Лидер в `local_crm_statuses` (PATCH-CRMF-LIDER-CRM-STATUSES-2026-06-15).
6. `_apply_perform_statuses()` — UPDATE статуса в `local_perform_leads` по нормализованному телефону из `local_leads_all`; 4 ветки crmf/mauto/plex/genzes (PERFORM_FUNNEL_2026-07-07-v5).
7. `_sync_vk_ads_stats()` — TRUNCATE+INSERT `local_vk_ads_stats_day` из FDW.
8. `_sync_account_specialist_roster()` — UPSERT specialist + UPDATE slepok + пересчёт robots (ROSTER_SYNC_2026-07-07).

## Архитектурная схема

```
ad_analytics (src) ──read──► step0 ──write──► ad_analytics_bi (dst)
                                                    │
                                                    ├── local_yandex
                                                    ├── local_leads_all
                                                    ├── local_domains
                                                    ├── local_gsheet_*
                                                    ├── local_crm_statuses
                                                    └── local_telega_in_orders (через FDW)

После INSERT — patches (порядок из run()):
   _patch_remove_elit_avto ──► DELETE FROM local_gsheet_sites WHERE salon='Элит Авто'
   _insert_pixel_pr_sites  ──► INSERT pixel_pr доменов в local_gsheet_sites
   local_gsheet_priezdi_marcar ──► UPDATE status в local_leads_all (продажи Маркар)
   _ensure_crmf_lider_crm_statuses ──► INSERT в local_crm_statuses
   _apply_perform_statuses ──► UPDATE local_perform_leads.status по телефону
   _sync_vk_ads_stats      ──► TRUNCATE+INSERT local_vk_ads_stats_day
   _sync_account_specialist_roster ──► UPSERT account_specialist_roster
```

Два соединения:
- `src_conn` → `ad_analytics` (только чтение)
- `dst_conn` → `ad_analytics_bi` (запись)

`FDW telega_in_orders` исключение: тянется через `dst_conn` (живёт в той же БД).

## Параметры

В `config/settings.py`:

| Параметр | Значение | Описание |
|----------|----------|----------|
| `DATE_FROM` | `'2026-01-01'` | Граница данных для `WHERE "Date" >= …` |
| `STREAM_CHUNK` | `100000` | Размер батча для `execute_values` |
| `TRUNCATE_INSERT_TABLES` | список пар `(src_table, local_table)` | Какие справочники тянем |
| `SRC_LEADS_ALL` | `'leads_all'` | Имя таблицы лидов в источнике (для фильтра по `created_date`) |
| `EXCLUDED_DOMAIN_IDS` | `(1645, 883)` | Не используется в step0 напрямую (применяется в step1) |

## Зависимости

- Python 3.10+
- `psycopg2` (потоковые курсоры server-side)
- Подключение к двум БД через `config/db.py` (`get_src_conn`, `put_src_conn`)

## Примеры запуска

```bash
# Только step0 (на Victory):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=0"

# В составе полного пайплайна:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py"
```

## Проверки после запуска

```sql
-- 1. local_leads_all содержит свежие данные
SELECT COUNT(*), MAX(created_date) FROM local_leads_all;

-- 2. local_perform_leads синкнут
SELECT COUNT(*), MAX(created_date) FROM local_perform_leads;

-- 3. Patch Маркар сработал
SELECT status, COUNT(*) FROM local_leads_all
WHERE source_type = 'marcar_crm_excel' AND status IN ('Продажа','Приехал','Дошел в КО','Одобрение')
GROUP BY status;

-- 4. VK Ads синкнут
SELECT COUNT(*), MAX(date) FROM local_vk_ads_stats_day;
```

## Инциденты и фиксы

- **14.04.2026 — задвоение `local_yandex`**: batch-коммиты в `_stream_insert` оставляли данные после падения, retry дублировал. Фикс: TRUNCATE+INSERT в одной транзакции.
- **`_check_yandex_needs_sync` использовал `>` вместо `!=`**: избыточные строки в dst не лечились. Фикс: любое расхождение count/max_date → resync.
- **2026-05-20 — `_patch_crm_statuses` отключён**: `local_crm_statuses` уже всегда верный, патч не нужен.
- **Marcar потери**: пытались патчить только `crm.marcar.ru`, не `plex-crm.ru` (см. `project_marcar_loss_root_cause_2026_05_20.md`).
- **VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17**: `_sync_yandex_full` отключена — все потребители переведены на FDW напрямую.
- **2026-07-10 — TRUNCATE завис на 75 мин** (`LOCK_TIMEOUT_GUARD_2026-07-10`): `TRUNCATE local_leads_all` молча ждал AccessExclusive-лок у двух осиротевших read-сессий. Фикс: `_truncate_with_lock_guard()` — lock_timeout 45 с, 4 ретрая с backoff, лог блокировщиков (pid/usename/query), RuntimeError вместо бесконечного зависания.

## Файлы

| Файл | Описание |
|------|----------|
| `step0.py` | Основной скрипт синхронизации (1783 строки) |
| `telega_in_orders_replacements.json` | Ручные замены UTM-полей для `local_telega_in_orders` (красные ячейки Google Sheets «посевы») |
| `__init__.py` | Пустой (модуль) |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |
