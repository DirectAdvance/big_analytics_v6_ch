# STEP.md — Шаг 7: Финализация ClickHouse

## Что Делает

Выполняет `OPTIMIZE TABLE ... FINAL` для:

- `ad_analytics.big_analytics_sources`
- `ad_analytics.big_analytics_calls`
- `ad_analytics.big_analytics_full`

## Поведение

| Случай | Действие |
|---|---|
| Таблица есть и это не `View` | count + `OPTIMIZE ... FINAL` |
| Таблица отсутствует | warning + skip |
| Объект является `View` | info + skip |

## Важно

PostgreSQL `SET LOGGED`, indexes и `VACUUM ANALYZE` относятся к v5/legacy и в BA6 step7 не выполняются.
