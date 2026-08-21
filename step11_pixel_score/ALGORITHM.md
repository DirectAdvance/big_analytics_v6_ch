# step11_pixel_score — алгоритм атрибуции (детали)

<!-- pixel-dedup-2026-08-17 -->
> **PIXEL_DEDUP / HYBRID_PIXEL_2026-08-20.**
> Старый `Пиксель_атрибуц` выведен из BA6-контракта; live-канон пикселя —
> `_source_table='pixel'`, `источник='Пиксель'`, `направление='Пиксель'`.
> Замер после accepted run `ed6bfc6f9c23`:
>
> | объект / ось | `_source_table` | строк |
> |---|---|---:|
> | `big_analytics_pixel_score` | `pixel` | 62 049 |
> | `big_analytics_full` — ось «По дате заявки» | `pixel` | 62 049 |
> | `big_analytics_full_arrival` — ось «По дате визита» | `pixel` | 30 019 |
>
> Визитную ось step13 читает из `big_analytics_pixel_score` напрямую и пишет
> `направление='Пиксель'`. Замер live ClickHouse: 2026-08-17.

> Вынесено из `CLAUDE.md` (2026-06-11) для соблюдения лимита ≤200 строк.
> Краткое назначение, вход/выход и схема — в [`CLAUDE.md`](CLAUDE.md). Здесь — формулы.

---

## Расчёт weight

Группировка кампании: `(month, салон, domain, источник, _source_table, CampaignId)`.

```
cr_composite = (1·kol_vo + 3·korr + 10·kval + 30·priezd + 100·prodazhi) / NULLIF(Clicks, 0)
weight       = cpl_score / NULLIF(SUM(cpl_score) OVER (PARTITION BY month, салон, domain), 0) × 100
```

`cr_composite` используется **только как фильтр** (кампании с cr_composite=0 исключаются, см. «Фильтр кампаний» ниже).
Знаменатель weight — `SUM(cpl_score)`, не `cr_composite`. (step11.py:248,564)

Коэффициенты воронки: `kol_vo=1, korr=3, kval=10, priezd=30, prodazhi=100`.

**Инварианты:**
- `SUM(weight) = 100.0` по (домену, месяцу) — когда есть хотя бы 1 кампания
- `направление` определяется по `_source_table` (технический маркер, не `источник`):

| `_source_table` | `направление` |
|----------------|---------------|
| `'direct'` | `'контекст'` (все tp1–tp7, включая РСЯ) |
| `'crop_targeting'` | `'посевы'` (Google Sheets VK/Telegram/MAX) |
| `'tp8'` | `'посевы'` (МК/ТК Telegram) |
| `'tp9'` | `'посевы'` (МК/ТК Max) |
| `'tp10'` | `'посевы'` (МК/ТК Telegram+Max) |

> **Почему не `источник`:** поле `источник` в `big_analytics_direct` — бизнес-метка (`'Контекст'`, `'SEO'`, `'telegram'`), никогда не равна `'direct'`. `_source_table` — технический маркер пайплайна, однозначен.

**Фильтр кампаний:**
- `_source_table IN ('direct', 'crop_targeting', 'tp8', 'tp9', 'tp10')`
- `CampaignId IS NOT NULL`, `domain IS NOT NULL`, `салон IS NOT NULL`
- `SUM(total_cost) > 0` AND `SUM(Clicks) > 0` — иначе cr_composite = NULL → исключение
- `cr_composite > 0` — кампании с нулевым CR не участвуют

Если у домена все кампании отфильтрованы — pixel остаток пишется через `_INSERT_LEFTOVERS_SQL` с `CampaignId=NULL, weight=100`.

---

## Расчёт cpl_score

Источник данных: `big_analytics_direct` (`_source_table IN ('direct','crop_targeting','tp8','tp9','tp10')`), данные за тот же месяц что и строка `pixel_score`. Для CPL используются **целые числа** из `big_analytics_direct`, не дробные значения из `big_analytics_pixel_score`.

### Шаг 1. Domain avg (по всем кампаниям домена за тот же месяц)

```sql
CPL_avg_квал    = SUM(total_cost) / NULLIF(SUM(kval), 0)       -- всегда если kval > 0
CPL_avg_визит   = SUM(total_cost) / NULLIF(SUM(priezd), 0)     -- только если SUM(priezd) >= 5
CPL_avg_продажа = SUM(total_cost) / SUM(prodazhi)              -- только если SUM(prodazhi) >= 3

cnt_avg_квал    = AVG(kval    FILTER WHERE kval > 0)
cnt_avg_визит   = AVG(priezd  FILTER WHERE priezd > 0)          -- только если SUM(priezd) >= 5
cnt_avg_продажа = AVG(prodazhi FILTER WHERE prodazhi > 0)
```

Пороги надёжности: `< 3 продаж по домену` → `CPL_avg_продажа = NULL`. `< 5 визитов по домену` → `CPL_avg_визит = NULL`. Если NULL → все кампании домена получают статус `'ждём'` по этому этапу.

### Шаг 2. Score каждого этапа (для кампании)

```
для каждого этапа (квал, визит, продажа):

  если CPL_avg_domain IS NULL:          ← домен не набрал порог данных
      статус      = 'ждём'
      score_stage = 1.0
      w           = 0.0

  если count_campaign > 0:
      CPL_campaign = total_cost / count_campaign
      score_stage  = clamp(CPL_avg_domain / CPL_campaign, 0.3, 3.0)
      статус       = 'данные'

  если count_campaign = 0:
      если total_cost < 3 × CPL_avg_domain:
          score_stage = 1.0
          статус      = 'ждём'       ← мало потрачено, ждём конверсий
      иначе:
          score_stage = 0.3
          статус      = 'плохо'      ← потрачено достаточно, результата нет
```

Clamp 0.3–3.0: пол защищает от штрафа ×10, потолок ограничивает "бесконечно хорошую" кампанию.

### Шаг 3. Вес этапа

```
статус='ждём'  → w = 0    (этап исключён)
статус='плохо' → w = 1.0  (минимум, участвует со штрафным score=0.3)
статус='данные'→ w = clamp(count_campaign / cnt_avg_domain, 1.0, 3.0)
```

Больше событий чем avg → выше вес → этап влияет сильнее. Диапазон 1–3 (не 0): активные этапы всегда вносят вклад.

### Шаг 4. Итоговый скор

```
cpl_score = clamp(SUM(w_i × score_i) / SUM(w_i), 0.3, 3.0)
```

Если все этапы `'ждём'` (SUM(w)=0) → `cpl_score = 1.0` (нейтральный).

### Шкала интерпретации

| cpl_score | Значение |
|-----------|---------|
| 2.5–3.0 | Отличная — CPL в 2.5–3× ниже avg домена |
| 1.5–2.5 | Хорошая — дешевле среднего |
| 0.8–1.5 | Средняя — около avg |
| 0.5–0.8 | Слабая — дороже среднего |
| 0.3–0.5 | Плохая — CPL в 2–3× выше avg или тратим много без результата |

---

## Pixel атрибутируется к кампании

### Атрибутируемые метрики (из pixel × weight/100)

```
total_cost      = pixel.total_cost      × weight / 100
kol_vo_zayavok  = pixel.kol_vo_zayavok  × weight / 100
korr            = pixel.korr            × weight / 100
kval            = pixel.kval            × weight / 100
priezd          = pixel.priezd          × weight / 100
prodazhi        = pixel.prodazhi        × weight / 100
```

### Не атрибутируемые метрики (принудительно 0)

`nekorr`, `ne_otvechaet`, `filtr`, `nedozvon`, `priedet`, `dohod_do_kredita`, `dobro`

### Маркеры строки в big_analytics_full

- `_source_table = 'pixel'`
- `источник = 'Пиксель'`
- `тип_заявки = 'Пиксель'`
- `направление = 'Пиксель'`
- `direction = 'Авто'`
- `key_pixel_score = "Date|domain|pixel|CampaignId"`

---

## Точность

`big_analytics_pixel_score` метрики **NUMERIC** → сумма точно совпадает с pixel.

`big_analytics_full` метрики **NUMERIC** → дробная атрибуция сохраняется точно, без построчного округления к int. (`COLUMNS_big_analytics_full.md:80-84`, `ROUND` в step11.py отсутствует)

> ⚠️ **Инвариант проекта:** дробную пиксельную атрибуцию НИКОГДА не приводить к int
> построчно — округление только у итоговой суммы (см. корневой `GOLDEN_BASELINE.md`).

**Инвариант (логируется в `details`):**

```
SUM(big_analytics_pixel.kol_vo_zayavok) ≈ SUM(big_analytics_pixel_score.kol_vo_zayavok)
SUM(big_analytics_pixel.total_cost)     ≈ SUM(big_analytics_pixel_score.total_cost)
```

---

## Перенос в big_analytics_full

Сначала keep-пересборка удаляет старые пиксельные строки
(`_source_table IN ('pixel','пиксель_атрибуц')`), затем INSERT канонического `pixel` из
`big_analytics_sources` без построчного округления метрик.
