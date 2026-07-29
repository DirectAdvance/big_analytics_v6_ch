# step6_build_full — Сборка `big_analytics_full`

Шаг 6 пайплайна. Финальный UNION ALL пяти источниковых таблиц + звонки → создаёт **главную таблицу проекта** `big_analytics_full`. Это та таблица, которую читает Power BI.

## Назначение

После шага 6 готова единая денормализованная таблица со всеми источниками трафика:
- Direct (контекст)
- SEO
- Посевы (Google Sheets + tp8 + Telega.in API + социальные)
- Отзывы (Яндекс)
- Звонки (по всем источникам, кроме посевов)

⚠️ Пиксельный трафик НЕ попадает в full на этом шаге — атрибутируется отдельно в step11.

## Архитектурная схема

```
big_analytics_direct ─┐
big_analytics_seo ────┤
big_analytics_crop ───┼──► UNION ALL ──► big_analytics_full (UNLOGGED)
big_analytics_reviews ┤                       │
raw_calls (inline) ───┘                       │
                                              ▼
                                     LEFT JOIN campaign_status
                                              │
                                              ▼
                                     CASE для emoji-префикса 🟢🟡⚪
                                              │
                                              ▼
                                  9+ постпроцессинговых UPDATE
                                  (campaign_status для звонков,
                                   направление='Контекст' для звонков,
                                   Название crm по салону,
                                   manager_login по домену + по салону,
                                   проджект по салону)
                                              │
                                              ▼
                                  → step7: SET LOGGED + индексы
                                  → load_reviews_to_big_analytics (перенос)
                                  → load_crop_to_big_analytics (доливка)
                                  → step11: атрибуция пикселей
```

## Схема колонок

Колонки фиксированы в `COLS` (~60 полей). Каждая ветка UNION ALL **обязана** возвращать колонки в том же порядке.

Группы колонок:
- **Ключи**: `key3`, `key_pixel_score`
- **Время**: `Date`, `День недели`, `week_start`
- **Кампания**: `CampaignId`, `CampaignName`, `AdGroupId`, `AdGroupName`, `AdNetworkType`, `Device`
- **Метрики**: `Impressions`, `Clicks`, `total_cost`
- **Атрибуты**: `domain`, `RlAdjustmentId`, `RlAdjustmentId_total`
- **Коды**: `campaign_code`, `tp`, `cpc_cpa`, `site_quiz`, `adgroup_code`, `ag_part1..7`, `fid`
- **Воронка**: `kol_vo_zayavok`, `korr`, `kval`, `priezd`, `prodazhi`, `nekorr`, `ne_otvechaet`, `filtr`, `nedozvon`, `priedet`, `dohod_do_kredita`, `dobro`
- **Атрибуция**: `account_login`, `manager_login`, `специалист`, `проджект`, `менеджер`
- **География**: `салон`, `город`, `регион`, `id_салона`
- **Сайт**: `тип_заявки`, `тип_сайта`, `шаблон`, `статус`, `Название crm`, `направление`, `direction`, `источник`, `поставщик`, `_source_table`
- **План**: `План заявки`, `План приезда`
- **Доп**: `марки авто`, `неверный_кодер_new`, emoji-префикс в названии, `priezd_arrival_date`, `prodazhi_arrival_date`

## Звонки (inline)

Звонки агрегируются НЕ в step3, а inline внутри step6:

```sql
SELECT ..., 'calls' AS _source_table, 'звонки' AS тип_заявки, NULL AS total_cost
FROM raw_calls
WHERE domain NOT IN (_CROP_DOMAIN_SUBQUERY)  -- 19 посевных доменов исключены
```

Воронка для звонков считается через `calls_agg_cases` из `local_crm_statuses`.

## Постпроцессинг (после CTAS UNION ALL)

| # | Действие | Цель |
|---|----------|------|
| 1 | Временные индексы (CampaignId/domain/salon/_source_table) + ANALYZE | 9 UPDATE'ов через index scan, не seqscan по 2.6M |
| 2 | UPDATE campaign_status='Активна' для звонков активных доменов | Звонки получают статус кампании домена |
| 3 | UPDATE направление='Контекст' для звонков с директологом | Звонки контекстных доменов получают направление |
| 4 | CREATE TEMP TABLE `_tmp_salon_aggs` (3 поля MAX по салону) | Подготовка для smart UPDATE |
| 5 | Один UPDATE: Название crm + manager_login + проджект по салону | 3 в 1 (3 seq scan → 1) |
| 6 | UPDATE manager_login по домену (для строк где нет в салоне) | Покрытие ~510 строк |
| 7 | Дроп временных индексов | step7 создаст финальные |

## Параметры

- `T_FULL` = `'big_analytics_full'`
- `WORK_MEM` = `'1GB'` (`SET LOCAL`)
- `COLS` — длинная строка колонок (фиксированный порядок)

## Зависимости

- step3 (5 источниковых таблиц)
- step4 (`campaign_status`)
- step0 (`local_gsheet_*`)
- raw_calls (из step1)

## Примеры запуска

```bash
# Только step6:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=6"

# Проверка после запуска:
psql -c "SELECT _source_table, COUNT(*), SUM(total_cost) FROM big_analytics_full GROUP BY 1 ORDER BY 2 DESC;"
```

## Проверки после запуска

```sql
-- Объём по _source_table
SELECT _source_table, COUNT(*) AS rows, SUM(total_cost) AS spend
FROM big_analytics_full GROUP BY 1 ORDER BY rows DESC;

-- Колонки в правильном порядке
SELECT column_name FROM information_schema.columns
WHERE table_name='big_analytics_full' ORDER BY ordinal_position LIMIT 20;

-- direction='Авто' (единственный интересный фильтр)
SELECT COUNT(*) FROM big_analytics_full WHERE direction='Авто';

-- Emoji-префикс работает (🟢 на активных)
SELECT campaign_status, LEFT("номер кампании | название кампании", 4) AS prefix, COUNT(*)
FROM big_analytics_full
WHERE _source_table='direct'
GROUP BY 1, 2 LIMIT 10;
```

## История фиксов

| Дата | Фикс |
|------|------|
| Апрель 2026 | `_move_tp8_to_crop` ставит `'tp8'`, не `'crop_targeting'` (иначе DELETE удалял 14k) |
| Май 2026 | 3 salon-UPDATE → 1 через `_tmp_salon_aggs` |
| Май 2026 | Временные индексы (CampaignId/domain/salon/_source_table) перед UPDATE'ами |
| 13.05.2026 | Emoji-префикс в названии кампании в outer SELECT |

## Связи

- **Зависит от:** step3 (5 источников), step4 (`campaign_status`), step1 (`raw_calls`)
- **После step6:** step7 (LOGGED+VACUUM+индексы), load_reviews, load_crop, step11, step12, step8

## Файлы

| Файл | Описание |
|------|----------|
| `step6.py` | Основной шаг (CTAS UNION ALL + 9 UPDATE) |
| `__init__.py` | Пустой |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |
