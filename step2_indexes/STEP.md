# STEP.md — Шаг 2: OPTIMIZE RAW

## Что делает

Выполняет `OPTIMIZE TABLE ... FINAL` для физических raw-таблиц ClickHouse из
`config.ch_settings.RAW_TARGET_TABLES`.

## Поведение

| Случай | Действие |
|---|---|
| Таблица есть и это не `View` | `OPTIMIZE TABLE ... FINAL` |
| Таблица отсутствует | warning + skip |
| Объект является `View` | info + skip |

## Почему Отдельный Шаг

Step1 пишет большие raw-таблицы батчами. Step2 уплотняет parts перед тяжёлыми запросами step3/step5.
