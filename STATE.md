# big_analytics_v6_ch — Состояние (handoff)

_Последнее обновление: 2026-08-14 (oleg_programmer — все Telegram-отправители v6 сведены на
`notifications/telegram.py`: 10 call sites в 9 файлах, единый sender+sanitize, verdict-first где это
gate, report-shape где это отчёт). Полная история → `STATE_ARCHIVE.md`._

## 2026-08-14 +05: все Telegram-отправители v6 на shared module (oleg_programmer)

- 10 call sites (9 файлов, director's re-count): `watch_pipeline.py`, `refresh_powerbi.py` (5
  sites incl. :437 raw-exception bug), `step_cron_night/pipeline_night.py` (already partially done
  round 1 — now sender too), `step8_stats/funnel_drift_snapshot.py`, `config/cookies.py`
  (`send_tg`/dead `send_tg_cookies_dead` removed, raw-exception bug at ensure_cookies fixed),
  `crm_mappings_check/check.py` (rewritten as `build_message`, dropped `!r` repr quotes + dead
  `unused` param), `yandex_direct_checking_report/report.py` (dropped 4090-char truncate — data
  loss bug, now safe chunking), `data_check/golden_reward.py` (`build_message` verdict gate),
  `data_check/reporter.py`. All raw `requests.post(...api.telegram.org...)` calls removed outside
  `notifications/telegram.py` (grep-verified). Format: verdict-first only where the kind is a
  gate (pipeline fail, parity, crm_mappings, golden_reward); reports (funnel drift, checking-report
  monospace table) kept report shape per Семён's judgement-call instruction — `<code>` kept ONLY
  for genuine fixed-width tables, not for exception text.
- `RUNBOOK.md`: added nohup+redirect example for night pipeline (none existed before); comment in
  `pipeline_night.py` now names the exact section instead of a vague "see RUNBOOK.md".
- Tests: `tests/test_telegram_notifications.py` — 18 tests (was 6), one per message kind +
  no-exception/no-repr/RU-money assertions. `golden_reward` test uses
  `pytest.importorskip(..., exc_type=ImportError)` — module has a PRE-EXISTING unrelated
  `ImportError: ARRIVAL_ROWS_MIN` from `data_check/verify_big_analytics.py` (confirmed present
  already in the parent commit, not introduced by this round) — not fixed, out of scope.
  `python3.9` (system) cannot import several of these files at all (`str | None` PEP604 syntax,
  pre-existing) — verified with `python3.11` instead (closer to Victory's `venv-v6`); 131 passed,
  1 skipped there, `python3.9` run: 130 passed, 1 skipped, 1 pre-existing collection failure
  (`watch_pipeline.py`, unrelated to this change).
- ⚠️ Found mid-session: an out-of-band auto-commit (`47b0335 "Improve BA6 telegram messages and
  direct feed docs"`, local-only, NOT pushed, 107 commits ahead of origin) bundled this round's
  edits together with unrelated pre-existing uncommitted changes (`STATE.md`, `PLAN.md`,
  `direct_feed_funnel/build.py`, `DB_TABLES.md`, `PBI_TABLES.md`, `README.md`, star_refactor docs).
  Did not touch git history myself (not asked, not my call) — flagging so it gets reviewed/split
  before anyone pushes.
- Not deployed, not run on Victory, no Telegram send performed (per task constraint) — render only.

## 2026-08-14 +05: direct feed light fact + compatibility view, полный pipeline OK (Codex)

- Безопасный `dim`-шаг: физическая `ad_analytics.fact_direct_feed_funnel` заменена на
  `ad_analytics.fact_direct_feed_funnel_light` + compatibility view с прежним именем
  `ad_analytics.fact_direct_feed_funnel`. Из физического факта вынесен только тяжелый
  `placement_feed_key`: хранится `placement_feed_key_hash`, view восстанавливает строку через
  `Dim_PlacementFeed`. Step 144 сам обновляет `Dim_PlacementFeed` перед созданием view, поэтому
  `--only-step=144` не зависит от предварительного step 146. `domain` и `account_login` оставлены в
  факте как низкорисковый компромисс.
- Код/доки: `direct_feed_funnel/build.py`, `direct_feed_funnel/README.md`,
  `tests/test_star_refactor_contracts.py`.
- Проверки до полного прогона: `py_compile` OK; `pytest tests/test_star_refactor_contracts.py -q` —
  14 passed. Первый `--only-step=144` выявил ClickHouse alias-конфликт в view CTE; alias исправлен,
  публичная view восстановлена вручную, затем `--only-step=146` прошёл OK. Позже review gate нашёл
  риск порядка сборки (`Dim_PlacementFeed` создавался позже view); добавлен вызов
  `build_dim_placement_feed(client)` внутри step 144 и регрессионный тест, после чего `pytest` —
  15 passed.
- Полный `pipeline.py` OK: `run_id=d68d74cb8465`, лог
  `logs/pipeline_full_after_feed_light_20260814_120525.log`, wall-clock по
  `data_quality_log` 129.0 мин, сумма duration шагов 130.3 мин. `verify_big_analytics` PASS;
  `KUDERKO_RAW_INCOMPLETE` остаётся информационным known issue #37.
- Самые дорогие шаги этого прогона: `step3` 3234.5с, `direct_spend_staging` 900.2с, `step1`
  897.6с, `step11` 640.4с, `build_star` 535.6с. Новая `direct_feed_funnel` часть: 48.7с.
- Сверка direct feed после полного прогона: `fact_direct_feed_funnel` view,
  `fact_direct_feed_funnel_light`, `pbi_import_fact_direct_feed_funnel` и
  `bi_fact_direct_feed_funnel` совпали по строкам и метрикам: 13,227,222 строк,
  cost=1,353,636,066.726184, clicks=33,567,405, impressions=732,221,435,
  forms=102,580, orders/korr=27,836, paid/prodazhi=6,262, `placement_feed_key` uniq=34,894,
  пустых placement key = 0. Размер light fact: ~180.26 MB; прежний full fact до правки был
  ~208.29 MB на 13,191,963 строках.
- После фикса порядка повторно выполнены `--only-step=144` (`run_id=47aebba230ea`), `--only-step=146`
  (`run_id=c87aed3666cb`) и `--only-step=147`; `verify_big_analytics` PASS. Финальная сверка direct
  feed: view/light/PBI/bi совпали — 13,246,925 строк, cost=1,355,729,860.099418,
  clicks=33,621,063, impressions=733,445,049, forms=102,742, orders/korr=27,836,
  paid/prodazhi=6,262, `placement_feed_key` uniq=34,929, пустых placement key = 0.
- Штатный `data_check/compare/run.py --json` по-прежнему не выполняет v5↔v6 числовую сверку:
  `exit 2`, контракт ожидает wide-колонки `специалист` и `"Название crm"` в
  `ad_analytics.fact_big_analytics`, а BA6 хранит их через dimensions (known issue #38).

## 2026-08-10 +05: полный pipeline OK, v5↔v6 compare/report, raw findings updated (Codex)

- Полный `pipeline.py` выполнен в foreground-сессии после проверки, что `nohup ... &` в текущем
  окружении убивает дочерний процесс при завершении shell. Первый час после корректного запуска не
  мониторился; дальше статус проверялся примерно раз в 15 минут.
- Прогон завершён `PASS`: `run_id=2ad6cc1dc880`, лог
  `logs/pipeline_full_20260810_1527_retry.log`, финал `2026-08-10 17:07:58 +05`.
  Итоговые строки: `raw_yandex=25,498,875`, `raw_leads=995,965`, `raw_calls=69,696`,
  `big_analytics_full=5,230,747`, `fact_big_analytics=5,351,549`.
- `verify_big_analytics` PASS, но `KUDERKO_RAW_INCOMPLETE` остаётся: 29/67 логинов Кудерко есть в
  raw, 28/67 до golden-отсечки. Это продолжение known issue #37, не регрессия ETL.
- Штатный `data_check/compare/run.py` сейчас падает до сверки чисел (`exit 2`): контракт ожидает
  wide-колонки `специалист` и `"Название crm"` прямо в `fact_big_analytics`, а текущая v6 хранит их
  через dimensions. Открыт known issue #38.
- Read-only custom compare v5 wide fact ↔ v6 `fact_big_analytics` + dimensions за
  `2026-02-01..2026-07-31`, `По дате заявки`: cost -0.06%, заявки -1.62%, квалифицированные -3.49%,
  продажи -0.86%. Крупные перекладывания: `Звонки`/`Контекст`/посевы/pixel, а не только totals.
- Raw-сверка: Direct в v6 богаче v5 на 338,059 строк и 13.19 млн cost; leads в 2026-срезе меньше
  на 2,155 и `updated_at` отстаёт до 2026-07-30; `raw_perform_leads` в v6 пустой; `domains` меньше
  v5 на 308. Отчёт: `V5_V6_RECONCILE_2026-08-10.md`; raw-кратко:
  `RAW_DIFF_FINDINGS.md`; JSON-артефакты в `logs/custom_compare_v5_v6_20260810_171446.json` и
  `logs/raw_compare_v5_v6_20260810_172639.json`.

---

_Предыдущее обновление: 2026-08-07 (oleg_programmer — Dim_Site rework по вердикту director
"needs rework" (блокеры A/B/C: направление/Название crm/специалист брались из СПРАВОЧНИКА с ДРУГОЙ
таксономией вместо факта). Исправлено, пересобрано вживую (shadow+swap), golden PASS без сдвига.
Полная история → `STATE_ARCHIVE.md`._

## 2026-08-07 +05: Dim_Site — фикс авторитетности колонок (направление/crm/специалист = факт, не
справочник), пересборка Dim_Site + pixel_score (oleg_programmer)

**Правка:** `star_refactor/build_star.py:139-372` (`build_dims()`, ключ `"Dim_Site"` в `ddl`-словаре),
маркер `DIM_SITE_COLUMN_AUTHORITY_FIX_2026-08-07`. `star_refactor/build_pbi_compat.py` НЕ трогал
(патч 1, уже принят).

- **Источник по колонкам (было единый — весь `raw_data.gsheet_sites`, стало по колонкам):**
  - `салон, город, регион, тип_сайта, шаблон, статус, проджект, менеджер, id_салона` (9) —
    **справочник авторитетен** (не изменено, 0 diff против текущего Dim_Site).
  - `направление, специалист, "Название crm"` (3) — **факт авторитетен** (новые CTE
    `fact_direction`/`fact_specialist`/`fact_crm`: `argMax` по `(count(), max(Date), value)` на
    непустых значениях `big_analytics_unified` по `site_key`); справочник — фолбэк ТОЛЬКО когда у
    домена нет ни одной непустой строки на факте. Механика: направление/crm_name — литералы,
    которые проставляет сама ETL (step3/step6), а не справочник; специалист — финальное значение
    после каскада `spec_fallback.py` (шаг 115, идёт ДО шага 145 build_star).
  - `домен` — решил НЕ менять источник (остался как раньше, по ветке): проверено live —
    для 1594 доменов, общих у справочника и факта, домен совпадает байт-в-байт в 100% случаев (0
    расхождений), это чистый join-key, не таксономия, риска канона нет.
- **Minor (детерминизм фолбэк-ветки, branch 2):** tie-break weight `tuple(count(), max(Date))` →
  `tuple(count(), max(Date), domain)` — добавлен `domain` третьим компонентом.
- **Diff словаря (полная сверка старый Dim_Site → новый, `git`-неизменный `star_refactor/build_star.py`
  диф — 234 строки, только этот файл):**
  - направление: `Авто 4679→3085` (1594 ключа вернулись в канон: `Авто→Контекст 1322`,
    `Авто→Комплекс 250`, `Авто→Пиксель_атрибуц 22` = 1594, ровно совпадает с числом из ревью director).
    Канон подтверждён: словарь теперь `Контекст/Комплекс/Пиксель_атрибуц/Авто(фолбэк)/…`, никакого
    молчаливого замещения канона отделами.
  - `"Название crm"`: 1570 site_key вернулись на канон (`One CRM→Фаиг 748`, `PLEX→Плекс 348`,
    `MEGA CRM→Мега 61`, `MarCar CRM→Маркар 50` и т.д.) — словарь теперь содержит `Фаиг/Плекс/Маркар/
    Мега` + доп. фактовые категории (`rivendell_excel/Ред Авто/Генезис/МаАвто`), фолбэк-остаток —
    сырые названия ПО из справочника только для доменов без фактовых данных.
  - специалист: пусто `3309→3025` (**+284 восстановлено, 0 новых потерь**, 19 заменено) — совпадает
    с before-bug baseline и требованием director (`gained=284, lost=0, replaced=19`).
  - 9 справочник-атрибутов: **0 diff** против текущего (уже верного) Dim_Site — не задеты.
- **Golden:** `verify_big_analytics.py` **PASS** до и после (cost=25 422 804.03, delta=+30.03,
  sales=57/floor 54) — бит-в-бит идентично, т.к. golden считается по `fact_big_analytics` напрямую,
  Dim_Site в расчёт не входит (подтверждено фактическим прогоном, не только по коду).
- **py_compile / tests:** `py_compile star_refactor/build_star.py` OK; `pytest tests/` — 85 passed.
- **Пересборка вживую:** `build_dims(client)` (shadow+swap) — `Dim_Site=4875` (swap OK, старый
  `Dim_Site_new` не остался). `Dim_Date`/`Dim_Campaign` тоже пересобрались штатно в том же вызове
  (не трогал их DDL, побочный эффект вызова общей функции). **`Dim_AdGroup` упал на
  `MEMORY_LIMIT_EXCEEDED` (512 МБ)** — это **известный, ДОКУМЕНТИРОВАННЫЙ pre-existing баг** (тот же,
  что зафиксирован в архивной записи 2026-08-06 «шаг 145 упал на Dim_AdGroup»), вне периметра этой
  задачи (только Dim_Site), не трогал; пустой недостроенный shadow `Dim_AdGroup_new` подчистил
  (`DROP TABLE`, старый живой `Dim_AdGroup` цел, 208 435 строк).
- **pixel_score пересобран** (`build_pixel_score()`, shadow+swap, 8.8 сек, 233 204 строки — без потерь
  строк). Направление в `pixel_score` до пересборки было ещё «старое хорошее» (`Контекст 169309/
  Комплекс 62101/Пиксель 1794` — ровно те цифры, что director спрогнозировал как «отложенный эффект»,
  ещё не материализовавшийся), после пересборки на исправленном Dim_Site — `Контекст 225065/Комплекс
  7648/Пиксель_атрибуц 491`, сумма та же 233204, **ни одного значения `Авто`** (коллапс в отдел не
  произошёл). Распределение внутри канона сдвинулось — ожидаемо: Dim_Site — это дименшн уровня
  домена (одно значение на домен), для доменов со смешанным трафиком (Контекст+Посевы) теперь
  побеждает fact-majority по колонке отдельно, а не одна случайно выбранная строка целиком, как было
  раньше (даже до бага) — архитектурно корректнее, не регресс.
- **Что НЕ трогал:** `star_refactor/build_pbi_compat.py` (патч 1, принят ранее, живёт как
  незакоммиченная правка в рабочем дереве — НЕ моя правка этой сессии); `Dim_AdGroup` DDL/бага
  MEMORY_LIMIT (вне периметра); полный прогон `pipeline.py` НЕ запускал (по прямому запросу); коммит
  НЕ делал.
- ⚠️ **Воспроизводимость по-прежнему нарушена**: в рабочем дереве множество чужого незакоммиченного
  рефакторинга помимо моей правки (см. `git diff --stat` — `build_pbi_compat.py`, `step6.py`,
  `pipeline.py` и др. уже были не по нулям ДО этой сессии) — не мой скоуп чистить.
- **Открыто:** `Dim_AdGroup` `MEMORY_LIMIT_EXCEEDED` (нужен цельный прогон/fix памяти, отдельная
  задача); v5↔v6 сверка контуров (`data_check/compare/run.py`) и визитная ось без fallback'а —
  не проверялись в этой сессии (вне периметра, см. архив).

---

_Более старые записи → [`STATE_ARCHIVE.md`](STATE_ARCHIVE.md) (Format B: STATE.md хранит только
последнюю запись, ротация по `.claude/rules/state-md-rotation.md`)._
