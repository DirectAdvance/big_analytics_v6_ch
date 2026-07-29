# Посевы (crop targeting) — как работает, что сделано, что проверять

> Рабочая записка по домену **посевов** в `big_analytics_v5` (БД `ad_analytics_bi`, Victory VPS).
> Составлено по итогам аудита **июнь 2026**. Цель — чтобы в следующий раз не разбираться с нуля.
>
> 📋 Детальный аудит проблем и план действий → [AUDIT_PLAN.md](AUDIT_PLAN.md)

---

## 1. Что такое «посевы»

Посев — это платное размещение рекламы в Telegram-каналах / VK-сообществах / Max-каналах
(не Яндекс.Директ). Закупка идёт двумя путями:
- **через платформу Telega.in** (API) — только Telegram-каналы;
- **прямой закуп** (Telegram / VK / Max) — заносится вручную в Google-таблицу.

Заявки с посевов помечаются в CRM как `utm_medium='posev'` (это **главный признак** посевного лида,
см. подводный камень ниже).

---

## 2. Источники данных (откуда что берётся)

| Таблица в БД | Что содержит | Период | Чем грузится |
|---|---|---|---|
| `gsheets_crop_targeting_account` | Ручной реестр расходов (Прямой закуп / ADBlogger / Telega IN). Дата TEXT `DD.MM.YYYY`. | **янв–апр 2026** (макс 29.04) | `step10_crop_targeting/load_crop_targeting.py` |
| `gsheets_crop_targeting_account_pravilo_utm` | **Справочник UTM**: `"UTM"` (сырой) → `"utm утвержденная"` (нормализованный ключ посева). Маппинг many→one. | — | `load_crop_targeting.py` (Лист1, клонирование строк по `\n`) |
| `local_telega_in_orders` | Заказы размещений через Telega.in API. Ключ `utm_campaign`. | янв–май+ | `step10_crop_targeting/load_telega_in_orders.py` |
| `crop_targeting_api_telegain_lead` | Зеркало расхода Telega.in (для big_analytics). | янв–май+ | `load_api_leads.py` |
| `local_leads_all` | Все заявки (CRM). Признак посева — `utm_medium='posev'`. | — | step0 |
| `big_analytics_full` | **Финальная витрина.** Посевы: `направление='посевы'`. | — | step3/step6 + step10 долив |

### Google-таблица справочника UTM
`https://docs.google.com/spreadsheets/d/1RgYaXiCgiipV1ljWFsiVYVzDQJZv1-V1w9hFdegQ0lI/edit?gid=0#gid=0`
- Колонка `UTM` → поле `"UTM"` (то, что реально стоит в `utm_campaign` ссылки канала; бывают опечатки/варианты).
- Колонка `utm утвержденная` → поле `"utm утвержденная"` (утверждённое имя посева для агрегации).
- Если в ячейке `UTM` несколько значений через перенос строки — `parse_utm_pairs()` клонирует строку.

---

## 3. Как данные текут в витрину (поток)

```
Google Sheet (Лист1: UTM, utm утвержденная)
        │ load_crop_targeting.py
        ▼
gsheets_crop_targeting_account_pravilo_utm   ← справочник: сырой UTM → ключ посева
        │
        ├─ load_crop_targeting_leads.py
        │     агрегирует расход+лиды по "utm утвержденная" + месяц
        │     → gsheets_crop_targeting_account_leads
        │
        ├─ step3.py
        │     • leads_direct CTE — ИСКЛЮЧАЕТ посевы из директа прямыми UTM-фильтрами:
        │         utm_source IN ('telegram','vk','max','vk_groups','stories_tg',...) + utm_medium IN ('posev','paid_social')
        │     • _add_social_posev_to_crop_sql — кладёт VK/Max заявки в crop (total_cost=0)
        │     • _move_tp8_to_crop — переносит tp8 (Яндекс МК/ТК) в crop с _source_table='tp8'
        │
        └─ step10_crop_targeting/load_crop_to_big_analytics.py
              долив расхода посевов в big_analytics_full (_source_table='crop_targeting')
```

### Как посевы лежат в `big_analytics_full` (`_source_table`)

| `_source_table` | Что это | Расход |
|---|---|---|
| `crop_targeting` | Закупка каналов (gsheets до мая + Telega.in API с мая) | есть |
| `tp8` | Яндекс.Директ на МК/ТК-кабинеты (`CampaignName='tp8_...'`) | есть (Яндекс) |
| `telegram` | Telegram-посевы (старый учёт) | есть |
| `social_посевы` | **VK/Max заявки-сироты без расхода** | **0** |

### ⚠️ Ключевой механизм дедупликации расхода
Расход НЕ задваивается **только** за счёт **временной границы 1 мая 2026**:
- **до 01.05** → расход берётся из `gsheets_crop_targeting_account` (ручной реестр);
- **с 01.05** → из Telega.in API (`crop_targeting_api_telegain_lead`).

Никакого JOIN/anti-join между двумя источниками НЕТ. Граница держится на том, что
gsheets-реестр обрывается на 29.04 (0 строк с датой ≥ 01.05).

---

## 4. Что сделано в этой сессии (июнь 2026)

### Удалена мёртвая таблица `leads_crop_attribution`
- Таблица создавалась (`step3._ensure_crop_attribution`), но **никогда не заполнялась** (`n_tup_ins=0`).
- Планируемый скрипт `attribute_leads.py` не был написан.
- `LEFT JOIN` к ней в `leads_direct` был **no-op** (пустая таблица → условие всегда TRUE).
- Реальное исключение посевов из директа делают **прямые UTM-фильтры** в `leads_direct`.
- Проверено: пересечение лидов direct ∩ crop = **0** (двойного учёта лидов нет).

**Удалено из кода (задеплоено на Victory):**
- `step3.py`: импорт `T_LEADS_CROP_ATTR`, no-op `LEFT JOIN` + его `WHERE`, функция `_ensure_crop_attribution()` и её вызов.
- `config/settings.py`: константа `T_LEADS_CROP_ATTR`.
- Документация: `DB_TABLES.md`, `PLAN.md`, `step3_build_sources/STEP.md` + `README.md`.

**Осталось сделать вручную на Victory (НЕ выполнено):**
```sql
DROP TABLE IF EXISTS public.leads_crop_attribution;   -- осиротевшая пустая таблица
```

---

## 5. Результаты аудита консистентности (июнь 2026)

| Вопрос | Вердикт | Детали |
|---|---|---|
| Дубли расхода (Telega.in vs gsheets за апрель) | ✅ НЕТ | Граница 1 мая разделяет источники идеально. Апрель в витрину идёт только из gsheets. |
| Полнота расхода в `big_analytics_full` | ✅ полный | До мая = gsheets (893 стр / 10.32M ₽), май+ = API (214 стр / 2.18M ₽). Суммы сходятся точь-в-точь. |
| Заявки матчатся с gsheets (янв–апр) | 1582 / 2208 | 626 не матчатся: month-mismatch 355, orphan-UTM 255, пустой UTM 16. |
| Заявки матчатся с telega (май+) | 547 / 589 | 42 не матчатся: VK/Max не идут через Telega.in API. |
| Заявки-сироты (нет расхода нигде) | ⚠️ 318 | Попадают в витрину как `social_посевы`, `total_cost=0`. |

### Открытые проблемы (на будущее)
1. 🔴 **VK/Max расход с мая отсутствует.** gsheets-реестр оборвался апрелем, Telega.in отдаёт только Telegram.
   VK/Max заявки висят в витрине с `total_cost=0`. Решение: либо вести VK/Max-расход дальше (реестр/отдельный источник),
   либо явно признать `social_посевы` лидами без расхода by design.
2. 🟠 **Февральский всплеск сирот (197)** — выбивается из ряда (янв 28 / фев 197 / мар 41 / апр 10). Разобрать конкретные каналы/UTM.
3. 🟡 **255 orphan-UTM (янв–апр)** — заявки с тегами, которых нет в `pravilo_utm`. Дозаполнить справочник или проверить UTM-разметку.

---

## 6. ⚠️ Подводные камни (читать перед проверкой!)

1. **НЕ матчить посевные лиды через `pravilo_utm`** — там есть мусорные записи `"UTM"='-'`, которые
   ловят ~27 000 директ/звонок-лидов. **Признак посевного лида = `local_leads_all.utm_medium='posev'`.**
2. **`big_analytics_full` пересоздаётся через CTAS (step6)**, а crop-долив (step10) идёт в самом конце pipeline.
   В момент пересборки таблица может быть пустой/неполной. Для валидаций — ждать стабилизации `COUNT(*)`.
3. **Расход без заявок — норма** (разместились, лидов не было). **Заявки без расхода — аномалия**
   (кроме `social_посевы` by design).
4. **Даты в gsheets — TEXT** `DD.MM.YYYY` / `D.M.YYYY`. Парсить: `TO_DATE("Дата", 'FMDD.FMMM.YYYY')`.
5. **Граница 1 мая 2026** — единственный механизм дедупликации. Если gsheets-реестр когда-нибудь продолжат
   заполнять за май+, ИЛИ Telega.in начнёт отдавать апрель — **появится двойной учёт расхода**. Это место проверять в первую очередь.

---

## 7. Что и как проверять в следующий раз (чек-лист + SQL)

> Все запросы — только чтение. Подключение: `ad_analytics_bi` на Victory (см. `.secret/.env` → `DB_VICTORY_*`).
> Период считать от `2026-01-01`. Признак посева в лидах — `utm_medium='posev'`.

### Проверка A — нет ли дублей расхода (главное)
Убедиться, что gsheets-реестр не залез в май, а Telega.in не отдаёт до-майские периоды в витрину.
```sql
-- 1. gsheets не должен иметь строк с датой >= 01.05
SELECT COUNT(*) FROM gsheets_crop_targeting_account
WHERE TO_DATE("Дата", 'FMDD.FMMM.YYYY') >= '2026-05-01';   -- ожидаем 0

-- 2. Расход crop_targeting в витрине по месяцам vs источники
SELECT date_trunc('month', "Date") m, _source_table, ROUND(SUM(total_cost)) cost, COUNT(*)
FROM big_analytics_full
WHERE направление='посевы'
GROUP BY 1,2 ORDER BY 1,2;
```

### Проверка B — полнота расхода (витрина = источники)
```sql
-- до мая: crop_targeting в витрине ≈ gsheets_leads; май+: ≈ зеркало API
-- сверить суммы помесячно, расхождение должно быть ~0
```

### Проверка C — заявки без расхода (нарушение инварианта)
```sql
-- посевные лиды
SELECT date_trunc('month', created_date) m, utm_source, COUNT(*)
FROM local_leads_all
WHERE utm_medium='posev' AND created_date >= '2026-01-01'
GROUP BY 1,2 ORDER BY 1,2;

-- сироты: посевной лид, по utm_campaign+месяц которого нет расхода
-- ни в gsheets (по pravilo_utm), ни в local_telega_in_orders
-- (см. логику аудита: matched / month-mismatch / orphan-UTM)
```

### Проверка D — заявки-сироты в витрине
```sql
SELECT _source_table, COUNT(*), SUM(kol_vo_zayavok) zayavki, ROUND(SUM(total_cost)) cost
FROM big_analytics_full
WHERE направление='посевы'
GROUP BY 1 ORDER BY 1;
-- social_посевы должен иметь cost=0 (это VK/Max сироты)
```

### Проверка E — справочник UTM жив
```sql
SELECT COUNT(*) total,
       COUNT(DISTINCT "UTM") uniq_utm,
       COUNT(DISTINCT "utm утвержденная") uniq_norm
FROM gsheets_crop_targeting_account_pravilo_utm;
-- ожидаем ~1850 строк, ~675 uniq UTM → ~656 utm утвержденная
```

---

## 8. Ключевые файлы кода

| Файл | Что делает |
|---|---|
| `step10_crop_targeting/load_crop_targeting.py` | Читает Google Sheet → `gsheets_*` + справочник `pravilo_utm` |
| `step10_crop_targeting/load_crop_targeting_leads.py` | Агрегация расход+лиды по `"utm утвержденная"` + месяц |
| `step10_crop_targeting/load_telega_in_orders.py` | Telega.in API → `local_telega_in_orders` |
| `step10_crop_targeting/load_crop_to_big_analytics.py` | Долив расхода посевов в `big_analytics_full` |
| `step3_build_sources/step3.py` | `leads_direct` (UTM-фильтры), `_add_social_posev_to_crop_sql`, `_move_tp8_to_crop` |
| `config/settings.py` | Имена таблиц |

Деплой big_analytics_v5 — **не через Mutagen**, а вручную:
```bash
scp work/big_analytics_v5/<файл> semen_vi@103.88.240.90:~/big_analytics_v5/<путь>
# (SSH alias victory не резолвится локально; пароль в .secret/.env → PASS, через expect)
```

---

_Последнее обновление: июнь 2026 (сессия аудита посевов + удаление leads_crop_attribution)._
