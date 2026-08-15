# big_analytics_v6_ch — статус

_2026-08-15, после оптимизации хранения и скорости прогона. История — `git log -p STATE.md`._

## Где мы сейчас

Прогон целиком **31-32 минуты** (было 130), вес `ad_analytics` **1727 МиБ** (было 2449).
Последний прогон `run_id=1ec561a8c705`, 29 шагов OK, `verify=OK`, принят `director`.
План и все замеры — `OPTIMIZATION_PLAN.md`.

## Не сделано / ждёт решения Семёна

1. **Коммита нет.** Правки лежат в рабочем дереве: `config/ch_utils.py`, `step1_load_raw/step1.py`,
   `step3_build_sources/step3.py`, `star_refactor/build_star.py`, `star_refactor/build_pbi_compat.py`,
   `region_spend/build_region_spend.py`, `criterion_spend/build_criterion_spend.py`,
   `adformat_spend/build_adformat_spend.py`, `direct_feed_funnel/build.py`,
   `tools/table_weight_report.py`, 4 файла в `tests/`, `OPTIMIZATION_PLAN.md`, `RAW_DATA_REQUEST.md`.
   Репозиторий требует явной команды на коммит (правило в `CLAUDE.md` этого проекта).
   ⚠️ В индексе лежат ЧУЖИЕ изменения другой сессии — `.sqlfluff`, `migrations/01_init_schema.sql`,
   `sql/create_pbi_big_analytics_full_view.sql`, `sql/v_monthly_kpi_avto.sql`. В наш коммит НЕ брать.

2. **Задвоение пикселя на заявочной оси — ждёт решения.** `step11._rebuild_full_with_pixel()` льёт
   одни и те же лиды дважды: сырой копией `_source_table='pixel'` и распределённым
   `'пиксель_атрибуц'`. Июль: 17 825 заявок и 13.48 млн ₽ по каждой ветке. Архитектура старая,
   одинаковая в v5 и v6, к правкам 14-15.08 отношения не имеет. Golden это НЕ ловит —
   `GOLDEN_SOURCES` исключает оба тега, проверять только ручным диффом по `_source_table`.
   Варианты: убрать сырую копию с заявочной оси / не суммировать обе в отчётах / отложить.

3. **В v5 пиксель выключен с 12.08** (`work/big_analytics_v5/config/pixel_attribution.py`,
   `PIXEL_ATTRIBUTION_DISABLED = True`, «do not run until a follow-up command»). Поэтому сайт
   `seoadvanced.ru/work/` показывает ноль пикселя, а v6 — показывает. Пауза не закрыта решением.

4. **Фаза 3 плана (−85 МиБ) не начата** — упирается в ручной шаг: в Power BI Desktop добавить связь
   факта с `bi_Dim_PlacementFeed` и опубликовать датасет. До публикации не деплоить, иначе refresh
   упадёт.

5. **`RAW_DATA_REQUEST.md` не передан** владельцу `raw_data`. Просим завести у себя минус-фразы
   (API v5) и PagesReport площадок (Grid на куках) — тогда пайплайн перестанет ходить в Директ сам.

## Что важно знать, чтобы не наступить

- **Окно батча не должно пересекать границу месяца.** `PIPELINE_BATCH_DAYS=7`, но `range_batches`
  прибивает край к первому числу. Причина: `step11:63` считает веса пикселя за месяц от первого дня
  окна, а дни тянет за всё окно — на пересекающих окнах 11% пиксельной оси падало в `CampaignId=0`
  без разделения, при сохранных тоталах (golden молчал). Аварийный откат: `PIPELINE_BATCH_DAYS=1`.
- **`--from-step=` выше step3 после успешного прогона не работает**: шаг 148
  `cleanup_wide_intermediates` штатно удаляет `big_analytics_sources`, step11 падает на
  `UNKNOWN_TABLE`. Перепрогон любого пост-step3 шага = полный прогон (31 минута).
- **Инстанс ClickHouse — 2 vCPU / 8.33 ГБ**, серверный потолок 7.49 ГБ. Запрос сейчас ограничен
  2 ГБ (`SAFE_QUERY_SETTINGS`), `max_threads=2`. На месячных окнах step3 падает по памяти — неделя
  это потолок ширины.
- Golden-дельта `+531.85 ₽` по Кудерко — не новая, root-cause в KNOWN_ISSUES #37 (неполное сырьё),
  допуск в этом репо `GOLDEN_COST_TOL=1000`.

## Открытые дефекты прошлых сессий (не трогали)

`KNOWN_ISSUES.md` — 14 пунктов 🔴, из них актуальные для ближайшей работы: #37 (сырьё Кудерко),
#38 (`data_check/compare` устарел после перехода на star/light — v5↔v6 сверка числами не работает).
