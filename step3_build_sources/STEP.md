# STEP.md — Шаг 3: Source Store

## Что Создаётся

| Объект | Тип | Назначение |
|---|---|---|
| `ad_analytics.big_analytics_sources` | MergeTree | общий source store для direct/seo/crop/reviews |
| `ad_analytics.big_analytics_direct` | View | `_source_table IN ('direct','tp8','tp9','tp10')` |
| `ad_analytics.big_analytics_seo` | View | `_source_table='seo'` |
| `ad_analytics.big_analytics_pixel` | View | `_source_table='pixel'`, заполняет step5 |
| `ad_analytics.big_analytics_crop_targeting` | View | crop/VK/telegram/social branches |
| `ad_analytics.big_analytics_reviews` | View | reviews branches |

## Как Строится

1. Direct вставляется дневными батчами из `raw_yandex`.
2. Lead-ветки вставляются дневными батчами из `raw_leads`.
3. `vk_perform` добирается отдельной вставкой из raw leads.
4. Reviews временно импортируются из Victory PostgreSQL.
5. Shadow table меняется местами с боевой через `swap_shadow`.

## Главное

- Pixel здесь не строится.
- Воронка считается через `reference_data.crm_status_mapping` и code overrides.
- Порядок и набор колонок должны оставаться совместимыми со step6.
