# big_analytics_v6_ch — Состояние (handoff)

_Последнее обновление: 2026-08-05 (главная сессия: разбор расхождений v5↔v6 + план чинки). Полная история — `STATE_ARCHIVE.md`._

## Сессия: 2026-08-05 — расхождения v5↔v6, план P1–P13 — В ПРОГРЕССЕ

### Что сделано
- **Гейт сравнения контуров** `data_check/compare/` — 14 коммитов `74d9f6a..9512306`, 75 тестов. Запуск `.venv/bin/python3 data_check/compare/run.py`. Живой прогон: exit 1, 8 блокеров. Спека `docs/superpowers/specs/2026-08-05-v5-v6-funnel-comparison-design.md`, план `docs/superpowers/plans/2026-08-05-v5-v6-gate.md`.
- **P1** `e8e09df` — вернул ветки `direct_unmatched`/`direct_zero` (v6 собирал Директ от расхода, лид без пары исчезал). Замер: 22 010 + 14 111 строк, расход у обеих 0, golden Кудерко +36 обращений / +0 продаж. Принято director.
- **P2+P3** `e1433fc` — патч продаж Маркара (`MARCAR_GSHEET_STATUS_2026-08-05` в `step1_load_raw/step1.py`) + Плекс «Отказ клиента» `correct→qualified`. Замер: продажи +265, квал +6 510. Принято director.
- **Причины расхождений разобраны полностью** — ранжированы, с числами и адресами в коде (см. `KNOWN_ISSUES.md` и историю сессии).

### Что осталось / открыто
- **P4 + P5 — код готов, коммит `6998bc4`** (см. блок ниже; прогона не было). ⚠️ P4 закрыл только ~1/4 разрыва по `dohod_do_kredita` и НОЛЬ по `dobro` — остаток не в джойне, а в содержимом `raw_data.crm_status_mapping`.
- Задачи **P6** (визитная ось пересчёт, а не копия), **P8** (перенос corrections.py), **P9** (дроп `big_analytics_sources` ломает 5 вьюх), **P12** (паритет веток Директа: каскад, перекраска посевов, `источник` по статусу), **P13** (fail-fast на покрытие статусов marcar).
- **P7-A (VK Ads 90→4 аккаунта) — код готов, коммит `05c2148`, прогона не было.** Скоуп `VK_AUTO_ACCOUNTS_SQL` в `config/ch_settings.py`; применён в `star_refactor/build_star.py` (`_vk_ads_sql` + `build_vk_dims`) и `step10_crop_targeting/step10.py::_insert_vk_ads_costs`. Симуляция read-only: fact_vk_ads 31 231→760 строк / 90→4 акк / 5 639→98 баннеров / 13 510 302.68→1 360 664.29 ₽, заявки 304 без изменений; full `vk_ads` 7 320→145 строк / →1 360 664.61 ₽. Осталось 2 хвоста: (1) 95 110 ₽ — дыра в `raw_data.vk_ads_stats_day` (нет 2026-07-23..26 у 1090518071, в PG v5 есть); (2) воронка у `vk_ads` в full = 0, в v5 — 20 обращений через `leads_vk_agg` (отдельная задача).
- **P7-B (таблица фидов) — BLOCKED, кода нет.** `ad_analytics.fact_direct_feed_funnel` — агрегат по площадкам РСЯ, а не по фидам (`placement_feed_key` = `yandex_direct_report_rows.placement`). Порт v5 невозможен: в ClickHouse нет `yandex_direct_feeds_report` (v5: 1 029 502 строки), `yandex_direct_feed_urls` (9 712), `direct_global_feed_rules` (14) и CRM-базы shadow orders. Факт зафиксирован в докстринге `direct_feed_funnel/build.py` (маркер `FEED_FUNNEL_NOT_PORTED_2026-08-05`, НЕ закоммичен — в файле лежит чужой незакоммиченный рефакторинг).
- **P11 — полный прогон с шага 0 + сверка гейтом — ТОЛЬКО ПОСЛЕ ВСЕГО.**

### Что сломано / риски
- 🛑 **Нужен GRANT:** `migrations/02_status_mapping_ab_2026-08-05.py --apply` падает `ACCESS_DENIED` — у `clickhouse_avto` только `SELECT ON raw_data.*`. Рекомендация director: прогнать миграцию админом разово, постоянный грант не выдавать.
- 🛑 **Порядок обязателен: миграция A → потом код.** Прогон нового `step1.py` без применённой миграции даёт молчаливую просадку `priezd ≈ −646`, `prodazhi −6` — гард ловит только отсутствующий `crm`, но не статус. Откат A тоже обязан быть парным. Закрывается задачей P13.
- 🛑 **Перекладчик (вне репо) стирает данные.** Простаивал 6.5 недель (19.06→04.08). Прогон 05.08 08:23–09:12 долил январь (+34 492) и **снёс февраль (−45 246)** — замена партиции вместо дозагрузки, в логе `success`. `deal_type` испорчен по всему 2026: `Кредит` 186 685→277, `Наличные` 30 920→127. Его сверка сломана (опирается на исчезнувшую колонку `row_hash`, считает NOT NULL вместо значений). Текст заявки владельцу собран. **Прогон до починки зафиксирует потерю февраля.**
- В рабочем дереве ~55 файлов чужого незакоммиченного рефакторинга — не трогать, в коммиты не подгребать.

### Ключевые файлы/команды
- `.venv/bin/python3 data_check/compare/run.py` — гейт сравнения контуров
- `migrations/02_status_mapping_ab_2026-08-05.py --check|--apply|--rollback|--only=A|B`
- Журналы перекладчика: `raw_data.etl_runs`, `raw_data.migration_checkpoints`, `raw_data.reconciliation_results`

**2026-08-05 +05: v6_ch — P4 (CRM-скоуп reason) + P5 (салон в пиксельной атрибуции), коммит `6998bc4` (oleg_programmer):**
- **P4 `step3_build_sources/step3.py:221-270` (`REASON_CRM_SCOPE_2026-08-05`)** — `dohod_do_kredita`/`dobro`
  матчились по `lower(reason)` глобально по всем CRM; теперь кортеж `(crm, reason)`, как status-сторона.
  Замер на `leads_deduped` (created_date≥2026-01-01, 1 081 741 лид): dohod **49 105 → 45 484 (−3 621)**,
  dobro **30 152 → 30 152 (0)**. В срезе витрины (домен с `gsheet_sites.direction='Авто'`): dohod
  15 527 → 14 665 (**−862**), dobro 8 191 → 8 191 (**0**). Звонки (`raw_calls`): dohod 3 979 → 3 649, dobro 0.
  Единственная задетая причина — «Консультация» (plex/crmf/mauto, credit-сторона).
  ⚠️ **Ожидание задачи (−3 900 / −4 200) не подтвердилось.** Остаток разрыва с v5 — в СОДЕРЖИМОМ
  `raw_data.crm_status_mapping`: причины `Соскок`/`Сам свяжется`/`Оформлен`/`Перестал отвечает` помечены
  `category='approved'` per-CRM и дают ~8 200 dobro сами по себе. Джойном это не лечится, только справочником.
- **P5 `step11_pixel_score/step11.py:93-116, 240-243, 316-325` (`PIXEL_SALON_JOIN_2026-08-05`)** — вернул
  v5-ключ `(месяц, салон, домен)` в `PARTITION BY`, в `INNER JOIN score_weights` и в предикат `leftovers`;
  пол `greatest(1e-6, …)` заменён на v5-гейт `WHERE score > 0`. Замер (симуляция обоих вариантов
  помесячно на живом CH, `big_analytics_sources` отсутствует → подставлен `big_analytics_full`):
  строк **459 881 → 237 148**, `uniqExact(key_pixel_score)` **310 007 → 234 966**, обращения
  **168 310.540 → 168 311.179** при источнике **168 312.000**, расход 125 817 960 → 125 818 367 при
  источнике 125 819 025 — сумма сохранена и стала БЛИЖЕ к источнику, к int не приводится.
  Старая ветка дала ровно 459 881 строку = live-`ad_analytics.pixel_score` → симуляция верна.
- **Остаточные 2 182 дубля `key_pixel_score`** (0.9%) — не размножение строк: 252 домена из 535 имеют
  >1 салона в пикселе, а ключ `Date|domain|пиксель_атрибуц|CampaignId` салон не содержит (формула v5).
- **НЕ трогал:** status-воронку (SQL `korr/kval/priezd/prodazhi` побайтово идентичен HEAD — сверено
  диффом сгенерированного SQL), расход Директа, step6, corrections, `raw_data.*` (только SELECT),
  чужой незакоммиченный рефакторинг (в коммит вошли ТОЛЬКО мои ханки, `step11.py` в дереве
  по-прежнему содержит чужую правку `specialist_correction_expr`).
- **НЕ проверено:** прогон пайплайна и golden — запрещены задачей.

**2026-08-05 +05: v6_ch — правка A (продажи Маркара) + правка B (Плекс «Отказ клиента») — КОД ГОТОВ, БД ЗАБЛОКИРОВАНА:**
- **A (маркер `MARCAR_GSHEET_STATUS_2026-08-05`)** — порт v5 `_patch_marcar_statuses()` (v5 step0.py:1228).
  В v6 источник `raw_data.leads_all` — реплика CRM (writes нет), поэтому патч сдвинут в
  `step1_load_raw/step1.py` ВЫРАЖЕНИЕМ: `_marcar_patched_status_expr()` + LEFT JOIN на
  `raw_data.gsheet_priezdi_marcar` в `_raw_leads_select_sql` И `_raw_calls_sql` (в v5 UPDATE
  накрывал local_leads_all целиком, вместе со звонками). Приоритет `Продажа>Дошел в КО>Одобрение>Приехал`,
  вниз по воронке не перезаписывает.
  ⚠️ Побочка, которую поймал тест: третий JOIN заставил анализатор CH назвать колонку `l.id` вместо
  `id` → `raw_leads.id` переименовалась бы и step3 упал. Лечится явным `l.id AS id` (в обоих селектах).
  Схема raw_leads/raw_calls/raw_perform_leads сверена с версией из HEAD — имена и типы идентичны.
- **B (маркер `PLEX_OTKAZ_QUALIFIED_2026-08-05`)** — 47 строк `plex`/«Отказ клиента» `correct`→`qualified`
  (паритет с v5, решение Семёна). Вторая половина A — 3 строки `marcar`: «Продажа»→sale,
  «Дошел в КО»/«Одобрение»→visit (в CH-маппинге нет general-ветки, поэтому без них патч статусов немой).
- **Обе правки справочника — в `migrations/02_status_mapping_ab_2026-08-05.py`** (`--check` / `--apply` /
  `--rollback` / `--only=A|B`), откат одной командой.
- **🛑 БЛОКЕР:** `--apply` падает `ACCESS_DENIED`: у `clickhouse_avto` только `GRANT SELECT ON raw_data.*`
  (полные права — лишь на `ad_analytics.*`). Нужна ОДНА внешняя операция под админом:
  `GRANT SELECT, INSERT, ALTER UPDATE, ALTER DELETE ON raw_data.crm_status_mapping TO clickhouse_avto;`
  (или прогнать миграцию админским пользователем). Другой креды в `.secret/.env` нет.
- **Замерено read-only (симуляция всех веток витрины на живом CH, прогона НЕ было), created_date≥2026-01-01:**
  A: prodazhi 3713→3978 (**+265**), priezd +64, kval +54, korr +50, nekorr −50, заявок 0, строк 0.
  B: kval 58 904→65 414 (**+6510**), korr/priezd/prodazhi/заявки/строки — **ровно 0**.
  Вложенность `korr≥kval≥priezd≥prodazhi` — OK во всех ветках после обеих правок.
- **НЕ трогал:** `_raw_yandex_sql` (расход), step3/step5/step6, corrections, любые таблицы кроме
  `raw_data.crm_status_mapping` (и та не изменена — блокер). `build_pixel.py` не патчил: патченых
  лидов Маркара в pixel-ветке 0 (замерено).
- **НЕ проверено:** прогон пайплайна и golden (запрет в задаче); эффект в `fact_big_analytics`
  появится только после step1 → step3 → corrections → step5/6 → build_star.

**2026-08-05 +05: v6_ch — возвращены две потерянные ветки лидов Директа (oleg_programmer):**
- **Баг:** `step3_build_sources/step3.py::_build_direct_sql` собирает Директ ОТ РАСХОДА
  (`FROM yd LEFT JOIN la ON la.key3 = yd.key3`). Лид, чьего key3 нет в статистике Директа, и лид
  без campaign_id (key3 `…|0|0|0|0`) не порождали ни одной строки — исчезали из витрины.
- **Фикс (маркер `DIRECT_LEAD_BRANCHES_2026-08-05`):** две ветки ОТ ЛИДА через
  `_build_lead_source_sql` — `_build_direct_unmatched_sql` / `_build_direct_zero_sql`,
  `_source_table='direct_unmatched'/'direct_zero'` (ровно то, что ждёт `GOLDEN_SOURCES`),
  `total_cost/Impressions/Clicks = 0`. Общий предикат direct-универса вынесен в
  `_direct_lead_universe_filter()` — одно определение на три ветки.
- **Гейт `gs.direction = 'Авто'` (строгое равенство, NULL исключается)** воспроизводит v5-гейт
  `FROM big_analytics_direct WHERE direction='Авто'` (v5 step6.py:114). Без него ветки притащили бы
  ~273 тыс. лидов доменов, которых нет в gsheet_sites (domain_id IS NULL в CRM).
- **Доказано read-only на живом CH (прогона НЕ было):** direct_zero 22 010 строк / 22 010 обращений /
  korr 12 223 / kval 1 959 / приезд 1 364 / продажи 115; direct_unmatched 14 111 / 14 111 / 6 564 /
  1 403 / 1 116 / 84. Пересечений: 0 с веткой direct (`key3 ∈ raw_yandex` = 0 строк), 0 с direct_zero,
  0 с crop_targeting. Расход новых строк = 0; SQL веток direct/seo/crop после рефактора
  ПОБАЙТОВО идентичен (сверено с `git show HEAD:` версией). Golden-срез Кудерко: +36 обращений,
  +0 продаж, +0 ₽. Дневной батч == глобальное окно (сверено на 2026-03-15).
- **Отличие от v5 by design:** каскад `CASCADE_MATCH_2026-07-03` не портирован — 6 479 строк, которые
  v5 подобрал бы в `direct`, в v6 остаются в `direct_unmatched` (потери/задвоения нет, только срез).
- **НЕ трогал:** ветку direct, seo, crop, звонки, `recreate_source_views` (вью `big_analytics_direct`
  по-прежнему = direct/tp8/tp9/tp10), step6, corrections.
- **НЕ проверено:** прогон пайплайна и golden (запрет в задаче); эффект на `fact_big_analytics`
  появится только после step3 → corrections → step6 → build_unified → build_star.

**2026-08-05 +05 (пред.): v6_ch — rivendell CRM mapping fix + гард класса бага (oleg_programmer):**
- **Баг:** `step3_build_sources/step3.py::_crm_expr` не знал `rivendell_excel` и молча self-мапил его
  (`else source_type`) в ключ `'rivendell_excel'`, которого нет в `raw_data.crm_status_mapping`.
  В CH-маппинге НЕТ general-ветки (в отличие от v5) → вся воронка CRM обнулялась.
  Факт по живой БД: `raw_leads` 5 281 лид rivendell, korr/priezd/prodazhi = 0/0/0.
- **Фикс:** словарь `CRM_BY_SOURCE_TYPE` (8 source_type, сверен с живой БД), `_crm_expr` генерится из
  него; фолбэк — `replaceRegexpOne(source_type, '(_crm)?_excel$', '')` вместо self-map.
  Маркер `CRM_MAP_RIVENDELL_2026-08-05`.
- **Класс бага закрыт:** `check_crm_mapping_coverage(client)` в начале `run()` — WARNING на неизвестный
  source_type и ERROR + строка в details, если выведенный ключ отсутствует в `crm_status_mapping`
  (с числом строк). Шаг не роняет. Проверено симуляцией регрессии.
- **Доказано read-only (без прогона):** rivendell BEFORE korr 0 / priezd 0 / prodazhi 0 →
  AFTER 4 427 / 78 / 6; ключи остальных 7 source_type не изменились (crmf-воронка идентична);
  итог по всем лидам сдвинулся ровно на дельту rivendell.
- **НЕ трогал:** маппинг marcar/genzes (решение Семёна), лейбл `Название crm` (`crm_by_domain`,
  step3.py:~395 и `step5_build_pixel/build_pixel.py:134` — там тот же пропуск rivendell, из-за него в
  витрине `Название crm='rivendell_excel'`; это нейминг = продуктовое решение).
- **GOAL 2 (расследование, без правок):** гипотеза «marcar/genzes мапинг неполный» НЕ подтвердилась:
  у marcar непокрыт 1 статус (`В работе - peretiazkaast`, 1 лид), у genzes — 0. `Корзина` у marcar
  (25 073 лида) и у genzes (12 912) в маппинге ЕСТЬ, категория `incorrect`. Разрыв v5↔v6 по продажам
  идёт не от отсутствующих строк, а от состава категории `sale`: marcar sale = только `COMPLETED`
  (11 лидов в raw_leads), genzes sale = `Продажа в кредит` (103) + `Продажа за наличные` (0).
- **CLAUDE.md** приведён к реальности (был Jul-31 текст «миграция НЕ выполнена, ETL на PostgreSQL»).
- **НЕ проверено:** прогон пайплайна и golden не запускались (запрет в задаче) — эффект на
  `fact_big_analytics` будет только после step3 → corrections → step6 → build_unified → build_star.
- **Ротация:** STATE.md был 77 КБ / 40 записей → перенесено 40 записей в `STATE_ARCHIVE.md`.
