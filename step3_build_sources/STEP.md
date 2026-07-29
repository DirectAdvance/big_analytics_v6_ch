# STEP.md — Шаг 3: Сборка таблиц по источникам

## Что создаётся

| Таблица | Источник | поставщик |
|---------|----------|-----------|
| `big_analytics_direct` | raw_yandex + raw_leads (директ) | `'Яндекс'` |
| `big_analytics_seo` | raw_leads (без UTM) | `'SEO'` |
| `big_analytics_pixel` | DDL-only в step3 (пустая), заполняется step5 | — |
| `big_analytics_crop_targeting` | DDL по образцу direct + посевы (GSheet, tp8/9/10, звонки/SEO 19 доменов, telegram, VK Ads) | разное |
| `big_analytics_reviews` | DDL по образцу direct (данные из `yandex_direct_reports_reviews`, обновляются вручную) | `'Отзывы'` |

Все таблицы создаются как `UNLOGGED` → финализируются в шаге 7 через `SET LOGGED`.

## Атрибуция лидов в big_analytics_direct

Лиды директ = все лиды, кроме:
- посевов (`leads_crop_attribution WHERE is_crop=TRUE`)
- SEO (`utm_source IS NULL OR utm_source = ''`)
- pixel (`utm_source LIKE 'victory_%'`)
- telegram-посевов (`utm_source IN ('telegram','stories_tg') AND utm_medium = 'posev'`)
- vk/tg storis-посевов (`utm_source IN ('vk_storis','telegram_storis') AND utm_medium = 'posev'`)
- VK Ads Комплекс/зазор/Перформ (`utm_source IN ('vkads', ...)` — уходят в crop через `_add_vk_ads_to_crop_sql`)

## big_analytics_direct — 4 части (UNION ALL)

| Часть | _source_table | Содержание |
|-------|----------------|------------|
| 1 | `direct` | Строки Яндекс с данными (DISTINCT ON key3) |
| 2 | `direct_unmatched` | Лиды без пары в Яндексе (нет key3 в raw_yandex, каскад тоже не нашёл) |
| 2b | `direct` (cascade_level заполнен) | Лиды с каскадным матчем (CASCADE_MATCH_2026-07-03), total_cost=NULL — расход не дублируется |
| 3 | `direct_zero` | Лиды без campaign_id, группируются по домен+дата |

## big_analytics_pixel

Таблица создаётся всегда, но в step3 остаётся пустой — `_build_pixel_sql()` делает только DDL
(CREATE + TRUNCATE), без INSERT. Заполняется отдельным шагом `step5_build_pixel/build_pixel.py`.

## Конфигурация work_mem

Перед каждой тяжёлой сборкой: `SET work_mem = '1999MB'` — чтобы sort/join не спиллились на диск.
