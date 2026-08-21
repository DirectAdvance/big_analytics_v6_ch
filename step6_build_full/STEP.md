# STEP.md — Шаг 6: `big_analytics_full`

## Что Делает

Собирает `ad_analytics.big_analytics_full` из:

- `ad_analytics.big_analytics_sources` без `_source_table='pixel'`;
- `ad_analytics.big_analytics_calls`, пересобранной в начале шага.

## Звонки

| Тип | Условие | Источник |
|---|---|---|
| обычные | `gs.direction='Авто'` и не посевной домен | `Контекст`/`SEO`/`SEO Flow` |
| посевные | crop-domain или `direction_main='Посевы'` | `Посевы_Звонки` |

## Публикация

Full пишется в `big_analytics_full_new`, затем публикуется через `swap_shadow`.

## Важно

- `SELECT *` не используется для вставки: колонки берутся из `column_names()`.
- `key_pixel_score` добавляется на этом шаге.
- Pixel не вставляется в full этим шагом.
