# CANON.md — канон значений `big_analytics_full`

> Эталонный справочник допустимых значений и инварианты витрины `public.big_analytics_full`.
> Отдельной lookup-таблицы наминга в проекте НЕТ — значения задаются императивно в SQL
> (step3 / step5 / step10 / corrections). Этот файл — единственный источник истины по канону.
> Вынесено из `CLAUDE.md` (lazy-load).

---

## Нормализация CampaignName перед извлечением campaign_code

Перед любым REGEXP_MATCH по `CampaignName` (для извлечения `campaign_code`, `tp`, `cpc_cpa`, `site_quiz`) — применяется двухступенчатая нормализация:

**Шаг 1 — кириллические lookalike-символы:**
В названиях кампаний встречаются кириллические символы, визуально идентичные латинским:
```sql
REPLACE("CampaignName", chr(1089), 'c')   -- кирил. 'с' (U+0441) → лат. 'c'
```
Симптом нарушения: `campaign_code = 'неверный кодер'` при корректно выглядящем названии (например, `tp7_cpс_site` где `с` — кириллическая).

**Шаг 2 — двойные подчёркивания (DOUBLE_UNDERSCORE_FIX_2026-07-03):**
```sql
REGEXP_REPLACE("CampaignName", '__+', '_', 'g')   -- __+ → _
```
Симптом нарушения: `campaign_code = 'неверный кодер'` при `CampaignName` вида `tp5_cpa__site — ...` (двойное подчёркивание). Пример: CampaignId 707635336, кампания Щербаковой.

**Где применено:** `step1_load_raw/step1.py` (маркеры `DOUBLE_UNDERSCORE_FIX_2026-07-03`). Оба шага выполняются в одном выражении до REGEXP_MATCH.

---

## Канон «направление»

Допустимые значения (рождаются в перечисленных местах):

| направление | Где задаётся |
|-------------|--------------|
| `Контекст` | step3 `_build_direct_sql` из `gs.status` (`Контекст активно`) + Block G2 (звонки с директологом) |
| `пиксель` | step5/step11 (пиксель-кампании); corrections Правило 8 для unmatched пиксель-лидов |
| `пиксель_атрибуц` | step11_pixel_score (атрибуция `big_analytics_pixel` → full) |
| `посевы` | step3 `_build_crop_sql` + step10 (Google Sheets посевы / Telega.in API) + step3 `_move_tp8_to_crop` (tp8/tp9/tp10 → направление='посевы', маркер TP9_TP10_POSEV_MOVE_2026-06-22) |
| `SEO` | step3 `_build_seo_sql` |
| `SEO Flow` | step3 из `gs.status` |
| `отзывы` | step3 `_build_reviews_sql` + load_reviews |

**ЗАПРЕЩЕНО:** техническое имя `utm_source` в колонке «направление» (например `victory_pxl`,
`victory_vdl` и т.п.). Все пиксель-источники → `'пиксель'`. Закреплено в
`corrections.py` Правило 8 `_rule8_utm_classify` (Вариант B, июнь 2026): блок `new_direction`
маппит весь кортеж `_UTM_PIXEL_SOURCES` в единое `'пиксель'`.

**NULL допустим** только для звонков без директолога (`_source_table='calls'`,
домен без `directologist` в `local_gsheet_sites` — см. Block G2 в `BLOCKS.md`). Для остальных строк NULL — баг.

---

## Канон «источник» — НИКОГДА не NULL

| источник | Где задаётся |
|----------|--------------|
| `Контекст` | step3 (direct) |
| `пиксель` | step5 / corrections Правило 8 |
| `пиксель_атрибуц` | step11_pixel_score |
| `звонки` | step6 inline (call-строки) |
| `telegram` | step10 (`"Источник"='Telegram'`) + step3 `_move_tp8_to_crop` (tp8, _source_table='tp8') |
| `Max` | step10 (посевы Max) + step3 `_move_tp8_to_crop` (tp9=Max/VK-ОК через Директ, _source_table='tp9') |
| `Telegram + Max` | step3 `_move_tp8_to_crop` (tp10=ЕПК, _source_table='tp10') |
| `VK` | step10 (посевы VK) |
| `SEO` | step3 `_build_seo_sql` |
| `контекст` (строчное) | строки отзывов (`_build_reviews_sql`) |

**ИНВАРИАНТ: `источник IS NOT NULL` для всех строк `big_analytics_full`.**

Ранее ~72 строки посевов имели `источник=NULL` (оператор не заполнил колонку «Источник»
в Google-таблице посевов). Фикс (июнь 2026) в
`step10_crop_targeting/load_crop_to_big_analytics.py` — источник резолвится через `COALESCE`:
1. явное значение из лида (`"Источник"`, `Telegram→telegram`);
2. мода источника из справочника `gsheets_crop_targeting_account` по `utm утвержденная`;
3. суффикс utm (`_vk`→VK, `_max`→Max);
4. дефолт `'telegram'` (посевы — преимущественно Telegram).

API-ветка посевов (≥ май 2026) источник выводит из URL канала — NULL там не бывает.

**Проверка инварианта:**
```sql
SELECT count(*) FROM public.big_analytics_full WHERE источник IS NULL;  -- ожидается 0
```

---

## Дата-граница: только с 2026-01-01

**ИНВАРИАНТ: `big_analytics_full."Date" >= '2026-01-01'`.** Строк раньше быть не должно.

- Источник константы: `config/settings.py` → `DATE_FROM = '2026-01-01'`.
- step1 копирует из источника только с `DATE_FROM`.
- `cleanup_old_dates` (в `pipeline.py` / `fast_pipeline.py`) удаляет «протёкшие» старые строки:
  `DELETE FROM public.big_analytics_full WHERE "Date" < DATE_FROM`.
  Нужно потому, что часть лидов 2025 года может затечь через ретро-обновления.

**Проверка инварианта:**
```sql
SELECT count(*) FROM public.big_analytics_full WHERE "Date" < '2026-01-01';  -- ожидается 0
```
