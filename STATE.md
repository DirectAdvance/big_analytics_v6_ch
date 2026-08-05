# big_analytics_v6_ch — Состояние (handoff)

_Последнее обновление: 2026-08-05 (oleg_programmer: возврат веток direct_unmatched/direct_zero). Полная история — `STATE_ARCHIVE.md`._

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
