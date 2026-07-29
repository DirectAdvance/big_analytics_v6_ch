# INDEX.md — вопрос → файл → якорь

> Гранулярный роутер по документации `big_analytics_v5`: «какой у меня вопрос» →
> «в каком файле ответ». Более детальный, чем «🗺 Карта документации» в [`CLAUDE.md`](CLAUDE.md):
> там — список файлов, здесь — список **вопросов**. Открывай нужный файл лениво.

---

## 🔢 Данные, цифры, расхождения

| Вопрос | Файл |
|--------|------|
| Почему цифра в дашборде разошлась с CRM/эталоном? | [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md) + [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) |
| Эталонные контрольные числа (Кудерко: расход/обращения/квалы/визиты/продажи) | [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md) §«Эталон» |
| Как правильно свериться (какая витрина, какая колонка, какой фильтр) | [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md) §«Как правильно проверять» |
| Готовый SQL золотой сверки | [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md) §«Эталонный SQL» · [`QUERIES.md`](QUERIES.md) |
| Известный баг / уже сталкивались с этим? | [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) |
| Допустимый дрейф свежести воронки (±единицы) | [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md) п.4 |

## 🎯 Атрибуция (исторически баг №1)

| Вопрос | Файл |
|--------|------|
| Как устроена атрибуция (единый авторитет) | [`ATTRIBUTION.md`](ATTRIBUTION.md) |
| Дробная пиксельная атрибуция, «никогда не int» | [`ATTRIBUTION.md`](ATTRIBUTION.md) · [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md) п.4 |
| «По дате заявки» vs «по дате визита» (`full` vs `_arrival`/`unified`) | [`ATTRIBUTION.md`](ATTRIBUTION.md) · [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) §8.9 |
| Нет двойного учёта лидов (direct ∩ crop = 0) | [`ATTRIBUTION.md`](ATTRIBUTION.md) · [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) §5 |
| TREATAS раздувает приезды в PBI «по визиту» | [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) #7 · [`ATTRIBUTION.md`](ATTRIBUTION.md) |
| Атрибуция по источникам (контекст/пиксель/посевы/SEO) | [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) §8.2 |

## 🗃 Витрина и колонки

| Вопрос | Файл |
|--------|------|
| Что значит конкретная колонка `big_analytics_full`? | [`COLUMNS_big_analytics_full.md`](COLUMNS_big_analytics_full.md) |
| Какой step пишет колонку / тип / единица (рубли vs доли) | [`COLUMNS_big_analytics_full.md`](COLUMNS_big_analytics_full.md) |
| Канон значений (направление / источник / даты / кириллица) | [`CANON.md`](CANON.md) |
| Воронка статусов (`local_crm_statuses`, маппинг метрик) | [`FUNNEL.md`](FUNNEL.md) |
| Схема всех таблиц БД + жизненный цикл | [`DB_TABLES.md`](DB_TABLES.md) |
| Таблицы, которые читает Power BI (10 шт.) | [`PBI_TABLES.md`](PBI_TABLES.md) |
| Технические блоки C–L, corrections | [`BLOCKS.md`](BLOCKS.md) |

## 🔄 Пайплайн, запуск, восстановление

| Вопрос | Файл |
|--------|------|
| Как перезапустить пайплайн / восстановиться после сбоя | [`RUNBOOK.md`](RUNBOOK.md) |
| Какой шаг в какой папке (карта шаг → папка) | [`PIPELINES.md`](PIPELINES.md#steps-map) (единый источник) · [`CLAUDE.md`](CLAUDE.md) §«Шаги пайплайна» (сжато) |
| 3 пайплайна, расписание, ночные шаги, токены | [`PIPELINES.md`](PIPELINES.md) · [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) §6 |
| Что делает конкретный шаг (текстом) | [`README.md`](README.md) §«Шаги пайплайна» |
| «Диск заполнен» / bloat / VACUUM | [`RUNBOOK.md`](RUNBOOK.md) §«Диск/bloat» |
| Свежесть данных / давно ли гонялся pipeline | [`RUNBOOK.md`](RUNBOOK.md) · [`QUERIES.md`](QUERIES.md) |
| Ретриггер refresh Power BI | [`RUNBOOK.md`](RUNBOOK.md) §«Refresh PBI» |
| Куки Я.Директ (glavpotok, prefetch, manager_login) | [`COOKIES.md`](COOKIES.md) |
| Точки отказа пайплайна | [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) §7 |

## ⭐ Star-схема

| Вопрос | Файл |
|--------|------|
| Статус cutover (что готово / что ещё на full) | [`README.md`](README.md) §«star-cutover» · [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) §4a |
| Колонки факта, измерения, связи, перенацеливание полей | [`STAR_REFACTOR_BRIEF.md`](STAR_REFACTOR_BRIEF.md) |
| Скрипты сборки/верификации star | [`STAR_REFACTOR_BRIEF.md`](STAR_REFACTOR_BRIEF.md) §«Скрипты» |

## 🧪 Проверки качества и SQL

| Вопрос | Файл |
|--------|------|
| Подсистема проверок (проджекты / поля / расход / воронка / свежесть) | [`data_check/README.md`](data_check/README.md) |
| Целостность маппингов `local_crm_statuses` | `crm_mappings_check/check.py` ([`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) §8.3) |
| Атрибуция продаж «Фаиг» (расход/маржа/прибыль) | [`sales_attribution/README.md`](sales_attribution/README.md) |
| Шпаргалка SQL (срезы, bloat, свежесть) | [`QUERIES.md`](QUERIES.md) |

## 🟡 Power BI

| Вопрос | Файл |
|--------|------|
| Правила правок PBIP (нужный файл, никакого int, верификация) | [`CLAUDE.md`](CLAUDE.md) §«Power BI / PBIP» |
| Какие таблицы и как PBI забирает данные | [`PBI_TABLES.md`](PBI_TABLES.md) |
| Запрет incremental refresh | [`CLAUDE.md`](CLAUDE.md) §«ЖЁСТКИЙ ЗАПРЕТ» |

## 🧭 Высокий уровень

| Вопрос | Файл |
|--------|------|
| Зачем проект, владелец, архитектура, инварианты | [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) |
| Откуда берётся каждая метрика (по источникам) | [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) §8 |
| Git-коммиты / триггеры | [`CLAUDE.md`](CLAUDE.md) §«Git» |
