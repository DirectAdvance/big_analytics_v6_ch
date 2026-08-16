# step11_pixel_score — Атрибуция pixel-воронки

<!-- pixel-dedup-2026-08-15 -->
> ⚠️ **PIXEL_DEDUP_2026-08-15 — описание ниже устарело в одном месте.**
> Атрибутированные пиксельные строки **больше не льются в `big_analytics_full`**: они дублировали
> те же лиды и расход, что несёт сырая копия `_source_table='pixel'` (дубль был 127 554 695.53 ₽).
> Как стало:
>
> | объект / ось | `_source_table` | строк |
> |---|---|---:|
> | `big_analytics_pixel_score` (физическая таблица) | `пиксель_атрибуц` | 241 313 |
> | `big_analytics_full` — ось «По дате заявки» | `pixel` | 31 151 |
> | `big_analytics_unified` — ось «По дате визита» | `пиксель_атрибуц` | 84 566 |
>
> Визитную ось step13 читает из `big_analytics_pixel_score` напрямую, поэтому она не пострадала.
> Код: `step11_pixel_score/step11.py:383`. Замер 2026-08-15.

Шаг 11 пайплайна. Распределяет pixel-воронку (`big_analytics_pixel`) по конкретным кампаниям через **CPL-скор** (`cpl_score`). Кампания с лучшим CPL-скором получает больше pixel. `cr_composite` (воронка / Clicks) используется только как фильтр отсечения: кампании с `cr_composite=0` исключаются из атрибуции (step11.py:242).

## Назначение

В отличие от direct/seo/crop — pixel-лиды НЕ привязаны к конкретной кампании (они привязаны только к домену через `utm_source`). step11 решает задачу атрибуции: распределить эти лиды по кампаниям того же домена пропорционально качеству трафика (CR).

## Выходные таблицы

| Таблица | Содержимое | Метрики |
|---------|-----------|---------|
| `pixel_score` | Атрибуционные веса (1 строка = кампания × месяц × домен) | `NUMERIC` |
| `big_analytics_pixel_score` | Атрибутированные строки (схема = `big_analytics_full`) | `NUMERIC` |
| `big_analytics_full` (`_source_table='пиксель_атрибуц'`) | Атрибутированные строки | `NUMERIC` |
| `big_analytics_full` (`_source_table='пиксель'`) | Прямой перенос `big_analytics_pixel` без атрибуции | `NUMERIC` |

## Архитектурная схема

```
big_analytics_full ──► campaign_monthly (direct+crop+tp8 за месяц)
                              │
                              ▼
                       cr_composite = (1·obr + 3·zay + 10·kval + 30·priezd + 100·prod) / Clicks
                              │ (фильтр: cr_composite=0 → кампания исключается)
                              ▼
                       cpl_score (0.3–3.0) — качество CPL кампании vs avg домена
                              │
                              ▼
                       weight = cpl_score / SUM(cpl_score по домену+месяцу) × 100
                              │
                              ▼
                       pixel_score (UNLOGGED → LOGGED)
                              │
big_analytics_pixel ─► pixel_daily JOIN pixel_score (на день, домен, месяц)
                              │
                              ▼
                       big_analytics_pixel_score
                       (метрики × weight/100, NUMERIC)
                              │
                              ▼
              (INSERT NUMERIC, без округления)
                              │
                              ▼
                       big_analytics_full (_source_table='пиксель_атрибуц')
```

## Формула CR composite и weight

```
cr_composite = (1·kol_vo_zayavok + 3·korr + 10·kval + 30·priezd + 100·prodazhi) / NULLIF(Clicks, 0)
               → фильтр отсечения: cr_composite=0 исключает кампанию (step11.py:242)

cpl_score    = clamp(0.3–3.0), качество CPL кампании vs avg домена по этапам (квал/визит/продажа)
weight       = cpl_score / SUM(cpl_score по домену+месяцу) × 100  (step11.py:248, :564)
```

Коэффициенты `cr_composite`: `kol_vo=1, korr=3, kval=10, priezd=30, prodazhi=100` — отражают важность шагов воронки. Знаменатель **weight** — `cpl_score`, а не `cr_composite`.

## Окно CPL-скора

**Per-month (не скользящее окно):** каждая строка `pixel_score` получает `cpl_score`, рассчитанный
по данным **того же месяца** что и строка. Апрель → domain avg апреля, март → март.
Разные месяцы одной кампании получают разные `cpl_score` независимо.

## Маркеры в `big_analytics_full`

| Поле | Значение |
|------|----------|
| `_source_table` | `'пиксель_атрибуц'` |
| `источник` | `'Пиксель_атрибуц'` |
| `тип_заявки` | `'Пиксель_атрибуц'` |
| `direction` | `'Авто'` |
| `key_pixel_score` | `Date|domain|пиксель_атрибуц|CampaignId` |

## Источники в weight (по `_source_table`)

| `_source_table` | `направление` в pixel_score |
|----------------|------------------------------|
| `direct` | `контекст` |
| `crop_targeting` | `посевы` |
| `tp8` | `посевы` |
| `tp9` | `посевы` |
| `tp10` | `посевы` |

> Используется `_source_table`, НЕ `источник` (бизнес-метка не однозначна).

## Точность

`big_analytics_pixel_score` — **NUMERIC** → сумма точно совпадает с `big_analytics_pixel`.

`big_analytics_full` — **NUMERIC** (без построчного округления к int) → дробная пиксельная атрибуция сохраняется точно (step11.py:666-670, :751-755). Погрешности ±1 нет.

Инвариант (логируется в `details`):
```
SUM(big_analytics_pixel.kol_vo_zayavok) ≈ SUM(big_analytics_pixel_score.kol_vo_zayavok)
SUM(big_analytics_pixel.total_cost)     ≈ SUM(big_analytics_pixel_score.total_cost)
```

## Не атрибутируемые метрики

`nekorr`, `ne_otvechaet`, `filtr`, `nedozvon`, `priedet`, `dohod_do_kredita`, `dobro` — принудительно 0 в pixel_score (без атрибуции).

## Параметры

| Параметр | Значение |
|----------|----------|
| `T_FULL` | `'big_analytics_full'` |
| `T_PIXEL` | `'big_analytics_pixel'` |
| `T_SCORE` | `'pixel_score'` |
| `T_OUT` | `'big_analytics_pixel_score'` |
| Коэффициенты | 1/3/10/30/100 |
| Окно | тот же месяц, что строка `pixel_score` (per-month) |

## Зависимости

- step5 (`big_analytics_pixel` заполнен)
- step6/step7 (`big_analytics_full` финализирован)

## Примеры запуска

```bash
# Только step11:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=11"

# Просмотр результата:
psql -c "
SELECT month, domain, COUNT(*), SUM(weight)
FROM pixel_score GROUP BY 1,2 ORDER BY 1 DESC, 2 LIMIT 20;
"
```

## Проверки после запуска

```sql
-- Инвариант: SUM(weight) = 100 по (домен, месяц)
SELECT month, domain, ROUND(SUM(weight)::numeric, 2) AS total_weight
FROM pixel_score GROUP BY 1,2 HAVING ABS(SUM(weight) - 100) > 0.5 LIMIT 10;

-- big_analytics_pixel_score метрики совпадают с pixel
SELECT SUM(kol_vo_zayavok) AS pixel_obr FROM big_analytics_pixel
UNION ALL SELECT SUM(kol_vo_zayavok) FROM big_analytics_pixel_score;

-- big_analytics_full содержит пиксель_атрибуц
SELECT COUNT(*), SUM(total_cost) FROM big_analytics_full
WHERE _source_table = 'пиксель_атрибуц';
```

## Старые таблицы (удалены автоматически)

step11 при запуске дропает старые таблицы:
- `analytics_pixel_score`
- `analytics_pixel_score_click`

## Связи

- **Зависит от:** step5 + step6 + step7
- **Используется:** Power BI страница "пиксель" (через `pixel_score.tmdl`)
- **Перед step8** — атрибуция должна быть готова к моменту отчёта

## Файлы

| Файл | Описание |
|------|----------|
| `step11.py` | DDL + INSERT + индексы + перенос в `big_analytics_full` + проверка инварианта |
| `__init__.py` | Пустой |
| `CLAUDE.md` | Детальная инструкция (формулы, BI-меры, инварианты) |
| `README.md` | Этот файл |
