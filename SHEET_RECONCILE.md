# Сверка с гугл-таблицей «посевы» — FINDINGS + METHODOLOGY

> Сверка консистентности данных big_analytics_v5 между тремя источниками:
> Google Sheets «посевы» (projects/sites, MSK UTC+3) ↔ PostgreSQL `ad_analytics_bi` (Victory) ↔ Power BI (свежесть).
> Прогон: **f77c29a5** · дата сверки: **2026-06-18** · данные свежие до **2026-06-17**.
> Источник проверки: агент `analytics_checker`. Повторяемая методология — в конце файла.

---

## FINDINGS

### Резюме
Прогон f77c29a5 — **golden PASS** (расход 25 422 798 ₽, эталон ±100; продажи 54). Инвариантов
воронки — **0 нарушений**. Свежесть PBI: последний step8 актуален. **Критических блокеров нет.**
Единственное, что требует внимания — **не пайплайна, а директологов/ввода данных** (см. ниже).

### Расшифровка алерта «698 проектов, ⚠️ нет 13»
Это **НЕ** счётчик клиентских проджектов. Блок «Покрытие аккаунтов» в `step8.py` считает
**login_key** (аккаунты Яндекс.Директ), а не домены/клиентов. Смысл: из 711 активных Авто-логинов
в `local_gsheet_sites` — 654 имеют расход в FDW (`total_cost > 0`), 57 не имеют. «нет 13» = 13
логинов, не попавших в `big_analytics_full` (подмножество 57 без расхода). Число «698» — это
кол-во доменов в big_analytics_full, совпало в строке случайно (разные метрики).

### Разбор 13 porg-* логинов

**Группа 1 — Удалён (5): НОРМА** (block_date заполнен, расхода и не должно быть)
| login_key | domain | block_date |
|---|---|---|
| porg-jgtsarpr | autocenter-avanta.ru | 04.06.2026 |
| porg-rpsw3psq | bucars-96.site | 25.05.2026 |
| porg-tcvp45ph | ladapark-ekb.ru / lada-yekaterinburg.ru | 29.09.2025 |
| porg-y44wdaar | bucar-kras.ru | 12.03.2026 |
| porg-ybl6hi25 | mixauto-surgut.ru | 07.05.2026 |

**Группа 2 — Запас (2): НОРМА** (сайты не в работе)
| login_key | domain |
|---|---|
| porg-dtkzhemq | ladapark-ber.ru |
| porg-iuc5t22r | multiauto-novosib.site |

**Группа 3 — «В работе» БЕЗ расхода (6): ВНИМАНИЕ ДИРЕКТОЛОГОВ** (реклама не крутится)
| login_key | domain(ы) | директолог | статус |
|---|---|---|---|
| porg-bmzx3zdm | autopark-196.site, ural-drive-cars.site | Саламахин Иван | В работе |
| porg-dihmkfjt | mixautomsk.ru | Саламахин Иван | На модерации |
| porg-dw7ov2yd | drivecar-msk.ru | Терехов Евгений | В работе |
| porg-odw5cl43 | bu-auto26.ru | Терехов Евгений | В работе (нет в FDW вообще) |
| porg-qfif3aby | rostov-autodrive.ru | Щербакова Наталья | В работе |
| porg-voekkrmy | budrive-ekb.site | Щербакова Наталья | В работе |

Данные: эти логины есть в FDW `yandex_direct_manager_reports` (кроме porg-odw5cl43 — нет вообще),
но `total_cost = 0` на все даты, `CampaignId = 0`. В `raw_yandex`/`big_analytics_*` — 0 строк.
**Это не баг матчинга доменов** (матчинг работает): аккаунт зарегистрирован, но кампании не
запущены / не тратят бюджет.

### Длинный список cost=0 (e-2007*, porg-*)
Полный список 57 логинов (711 эталон − 654 с расходом). Проверено выборочно (`e-20074386` →
`autopark-196.ru` с `.ru`, отдельный домен от `.site`): строки есть, `total_cost = 0`. Причина —
реальные нулевые расходы (остановленные кампании, SEO-трафик, пиксель без атрибуции Директа).
**Норма для большинства**, не баг матчинга. Исключение — 6 активных porg-* из группы 3.

### Покрытие доменов (Sheets ↔ PG ↔ big_analytics_full)
| Метрика | Значение |
|---|---|
| Доменов в local_gsheet_sites (Авто, активные) | 3 658 |
| Логинов (эталон step8, Авто активные) | 711 |
| Доменов Авто «В работе» | 886 |
| Домены «В работе» БЕЗ строк в big_analytics_full | 8 |
| └ без login_key (не подключены к Директ) | **2: budcars-surgut.ru, autotula-cars.ru** |
| └ с porg-* логином, но без расхода | 6 (группа 3 выше) |
| Distinct доменов в big_analytics_full | 1 415 |
| Distinct логинов в big_analytics_full | 1 009 |

Нюанс: big_analytics_full покрывает не только Авто (1 415 доменов vs 886 Авто «В работе») —
включены Digital/SEO Flow/Внутренний маркетинг + исторические строки закрытых доменов.

### Инварианты воронки
`korr < kval`: 0 · `priezd < prodazhi`: 0 · `kol_vo_zayavok < korr`: 0. Консистентна.

### Свежесть PBI
Последний успешный step8 актуален (~минуты на момент сверки), `stale = false`. Import mode — данные актуальны.

### Приоритизация
- **Блокер:** нет.
- **Внимание (директологи / ввод данных):**
  - 6 активных porg-* «В работе» без расхода (Саламахин / Терехов / Щербакова — см. группу 3) — уточнить, почему реклама не крутится.
  - 2 домена без login_key — вписать `login_key` в гугл-таблицу: `budcars-surgut.ru`, `autotula-cars.ru` (иначе step4 не получит статусы кампаний).
- **Норма:** 5 удалённых + 2 «запас» + длинный cost=0 список.

---

## METHODOLOGY

### Источник 1 — Google Sheets «посевы»
- Таблица ID `1wMAfpMyHEwa99NT0-kosTcA5Uny_r_6YXmbO5WKqlKc`, лист GID `1519720357`.
- Зеркалируется в PostgreSQL (Victory) в `local_gsheet_sites` через step0.
- Ключ домена — колонка `domain` (поиск по `_DOMAIN_KEYS`: сайт/домен/domain/site/url/адрес).
- Таймзона таблицы: **Москва UTC+3**. Мак/проверка — Екб UTC+5 (разница 2 ч). Поле `block_date`
  (`DD.MM.YYYY`) — без времени, зона не влияет.

### Источник 2 — PostgreSQL `ad_analytics_bi` (Victory)
- Доступ: `$DB_VICTORY_*` через `.secret/loader.py` (`load_db('victory')`). Не хардкодить.
- Ключевые таблицы:
  - `local_gsheet_sites` — зеркало таблицы, ключ `domain`, аккаунт = `login_key`.
  - `big_analytics_full` — финальная витрина, ключи `domain` + `account_login`.
  - `yandex_direct_manager_reports` — FDW Яндекс.Директ, ключ `account_login`.
  - `data_quality_log` — статусы шагов.
- Фильтр покрытия (Авто, активные):
  `direction='Авто' AND login_key IS NOT NULL AND login_key NOT IN ('','Нет') AND login_key ~ '^[a-z0-9]'
   AND (block_date='' OR block_date IS NULL OR TO_DATE(block_date,'DD.MM.YYYY') >= '2026-01-01')`.

### Источник 3 — Power BI
- Свежесть: `SELECT MAX(run_at) FROM data_quality_log WHERE step='step8' AND status='ok'`.
- Порог устаревания: `hours_ago > 24` = stale. Import mode — обновляется только после step8.

### Ключи сопоставления
- Sheets ↔ PG: `domain` (lower + trim).
- PG ↔ FDW: `login_key` = `account_login`.
- PG ↔ big_analytics_full: `domain` + `account_login`.

### Что расхождение, что норма
- **Расхождение:** домен «В работе» + нет строк в big_analytics_full + нет расхода в FDW = кампании не запущены (директологу).
- **Расхождение:** `only_in_sheets` (есть в Sheets, нет в local_gsheet_sites) = step0 не синхронизировал.
- **Расхождение:** `no_analytics_data` (есть в Sheets и БД, 0 строк в big_analytics_full) = pipeline не загрузил.
- **Норма:** «Удалён»/«Запас» без расхода; `only_in_db` (архивные/удалённые); cost=0 у остановленных/SEO/пиксель.

### Таймзоны при сравнении дат
- `block_date` (gsheet) — дата без времени → сравнивать без смещения.
- `run_at` (`data_quality_log`) — UTC; `NOW()` в PostgreSQL = UTC, для `hours_ago` безопасно
  (`EXTRACT(EPOCH FROM (NOW()-run_at))/3600`). В MSK +3 ч, в Екб +5 ч.
- Поля `Date` в big_analytics_full — дата без времени → сравнение корректно.

### Как повторить проверку
1. `data_check/run.py --json` — полный автоматический отчёт (5 блоков).
2. Ручная сверка porg-* логина: JOIN `local_gsheet_sites` по `login_key` (status/block_date/директолог)
   → JOIN `yandex_direct_manager_reports` по `account_login` (`SUM(total_cost)`)
   → JOIN `big_analytics_full` по `domain` (`COUNT(*)`).
3. Свежесть: один SQL к `data_quality_log`.
4. Инварианты воронки: `COUNT(*) FILTER (WHERE korr < kval)` и т.п. за 30 дней.
