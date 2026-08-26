# step4_campaign_status — Статусы кампаний Яндекс.Директ

Шаг 4 пайплайна `big_analytics_v6_ch`. Пересоздаёт ClickHouse-VIEW `campaign_status`/`campaign_status_v`
поверх `reference_data.direct_campaigns`, обогащая downstream-шаги (в первую очередь `step3`) колонками
`campaign_status` и `payment_model`.

⚠️ **Это не v5.** В v5 (`work/big_analytics_v5/`) step4 сам ходил в неофициальный Grid API
Яндекс.Директа через куки браузера, двухфазно, с фоновым потоком. В v6_ch этого больше нет:
`reference_data.direct_campaigns` уже приходит готовой (её наполняет отдельный внешний загрузчик вне этого
пайплайна), а `step4.py` — это просто `CREATE VIEW` поверх неё. Если ищете код получения статусов
через Grid API/куки — его в этой папке нет.

Содержит также подпапку `check_utm/` — файл лежит здесь, но **не используется в v6_ch** (см. ниже).

## Назначение

- **`ad_analytics.campaign_status`** / **`ad_analytics.campaign_status_v`** — VIEW со статусом
  кампании: 'Активна' / 'Остановлена' / 'Архив' (или исходный raw-статус, если не попал под
  маппинг)
- **`payment_model`** — passthrough одноимённой колонки из `reference_data.direct_campaigns` (значение
  вычисляется не здесь)
- Emoji-префикс статуса в названии кампании (🟢/🟡/🔴/⚪) для Power BI формируется не здесь:
  `step4.py` отдаёт только текстовый `campaign_status`, а визуальный префикс добавлен в PBIP
  semantic model `Большая аналитика_admin_ch/.../definition/tables/Dim_Campaign.tmdl`.
  Если префикс пропал в отчёте, проверять TMDL и публикацию/refresh, не `step4.py`.

## Что реально делает `step4.py`

```python
def prefetch_statuses(*args, **kwargs):
    """Compatibility hook for v5 orchestrators; v6 reads raw_data directly."""
    ...  # no-op, только логирует и возвращает None

def run(conn=None, run_id=None, prefetch_thread=None) -> dict:
    client = get_client()
    client.command("DROP TABLE IF EXISTS ad_analytics.campaign_status_v SYNC")
    client.command("DROP TABLE IF EXISTS ad_analytics.campaign_status SYNC")
    client.command("CREATE VIEW ad_analytics.campaign_status AS SELECT ... FROM reference_data.direct_campaigns")
    client.command("CREATE VIEW ad_analytics.campaign_status_v AS SELECT ... FROM ad_analytics.campaign_status")
    return {"rows": rows, "details": f"campaign_status_v={rows:,}"}
```

Никакого двухфазного prefetch/join, фоновых потоков, `ALTER TABLE ... ADD COLUMN`, `UPDATE`, а также
постпроцессинга по звонкам/направлению/crm/manager_login — этого кода в `step4.py` v6_ch нет.
`конн`/`run_id`/`prefetch_thread` в сигнатуре `run()` приняты для совместимости с оркестратором и не
используются (`# noqa: ARG001`).

## Архитектурная схема

```
reference_data.direct_campaigns (наполняется вне этого пайплайна)
        │
        ▼  step4.py: DROP + CREATE VIEW
ad_analytics.campaign_status  (multiIf по status/state → 'Активна'/'Остановлена'/'Архив')
        │
        ▼  CAST/LowCardinality поверх
ad_analytics.campaign_status_v
        │
        ▼  LEFT JOIN ON CampaignId (step3_build_sources/step3.py:1180, 1629)
step3 → ... → Dim_Campaign (campaign_status, payment_model из ad_analytics.big_analytics_unified)
```

`step4` в списке `pipeline.py::STEPS` идёт **перед** `step3` — порядок обязателен, иначе `step3`
не найдёт свежую `campaign_status_v`.

## Маппинг статуса (`multiIf` в `step4.py`)

| Условие | Результат |
|---|---|
| `status='ARCHIVED'` или `state='ARCHIVED'` | `Архив` |
| `status IN ('SUSPENDED','STOPPED')` или `state IN ('OFF','SUSPENDED')` | `Остановлена` |
| `status IN ('ACCEPTED','ACTIVE')` или `state='ON'` | `Активна` |
| иначе | исходное значение `status` как есть |

Регистр не важен (`upper(...)`). `state` важнее общего `status`: `status=ACCEPTED` +
`state=SUSPENDED/OFF` считается остановленной кампанией, иначе фильтр `Активна` в Power BI
покажет логины без реально активных кампаний. Это не тот набор ключей, что использовался в v5.

## Куки

⚠️ **`step4.py` v6_ch не импортирует `config/cookies.py` и не читает `cookies.json`** — статусы
кампаний в этот шаг приходят уже готовыми через `reference_data.direct_campaigns`, куки при построении
VIEW не участвуют.

`config/cookies.py` в проекте по-прежнему существует и используется, но **другими** модулями:
`check_utm/utm_direct_audit.py` в этой же папке (сам мёртвый код, см. ниже) и
`direct_placement_links/build.py`. Актуальные тестовые логины в `config/cookies.py::_TEST_LOGINS`
(используются в `check_cookies_alive()`/`check_all_cookies_strict()` — проверка живости через
**любой** из 3 аккаунтов, не через конкретные 2):
- `victoryagency-direct1618440` → `acbu-spb-436222-ns89`
- `victorylotsofads1` → `e-20074351`
- `victoryagency14` → `porg-7uhutcdh`

`config/cookies.py::send_tg()` — тонкий делегат к общему `notifications/telegram.py::send_html`
(сигнатура `text -> None` сохранена ради обратной совместимости — её использует
`ensure_cookies_alive_or_stop(send_tg=...)` как дефолтный callback, реально достижимый через
`direct_placement_links/build.py:712,714`). Функции `send_tg_cookies_dead()` в коде больше нет —
она была удалена как код с нулевым числом вызовов, не только что.

## Зависимости

- `reference_data.direct_campaigns` (ClickHouse; наполняется вне этого пайплайна, `step0` только
  проверяет, что таблица не пуста)
- Никакой зависимости от `step0`/`step3`/кук/Telegram-прокси внутри `step4.py` самого нет —
  зависимость от `reference_data.direct_campaigns` целиком внешняя

## Примеры запуска

```bash
# В составе полного пайплайна:
ssh victory "cd ~/big_analytics_v6_ch && ~/venv-v6/bin/python3 pipeline.py"

# Только step4:
ssh victory "cd ~/big_analytics_v6_ch && ~/venv-v6/bin/python3 pipeline.py --only-step=4"
```

`fast_pipeline.py`/`pipeline_powerbi.py` из v5 (упоминались раньше как "пропускающие Фазу A") в
`big_analytics_v6_ch` не существуют — здесь один оркестратор, `pipeline.py`.

## Проверки после запуска

```sql
-- Статусы/типы оплаты в VIEW
SELECT `статус`, payment_model, count()
FROM ad_analytics.campaign_status_v GROUP BY `статус`, payment_model;

-- Сколько кампаний вообще видно в raw-источнике
SELECT count() FROM reference_data.direct_campaigns;

-- payment_model заполнен (если NULL массово — проблема в raw-загрузчике, не в step4)
SELECT count() FROM ad_analytics.campaign_status_v WHERE payment_model IS NULL;
```

## Подпапка `check_utm/`

**Файл:** `check_utm/utm_direct_audit.py` — **мёртвый код в v6_ch**: 0 импортов из активных
модулей проекта (проверено `grep` по всему репозиторию). Это нетронутая v5-версия: `psycopg2`,
источник `public.big_analytics_direct`, официальный Direct OAuth API + Метрика, пишет в
`public.check_utm`/`public.check_utm_fuck_direct` (Postgres-таблицы) — ни один из этих объектов не
существует в текущем ClickHouse-контуре v6_ch.

Реальный ночной UTM-аудит в v6_ch — **другой файл, в другой папке**:
`step_cron_night/metrika_raw_builders.py::build_check_utm()`, источник —
`raw_data.metrika_yandex_utm_daily` + `reference_data.metrika_yandex_counters` + `reference_data.gsheet_sites`
(ClickHouse, без живых вызовов Direct/Metrika API на момент сборки). Пишет в shadow-таблицы
(`ad_analytics.check_utm_new`/`check_utm_fuck_direct_new`), затем swap → итог:
`ad_analytics.check_utm`, `ad_analytics.check_utm_fuck_direct`. Запускается через
`step_cron_night/step13_utm_direct_audit/run.py` (cron 03:00 МСК); этот `run.py` **не импортирует**
`check_utm/utm_direct_audit.py` из данной папки.

| | `check_utm/utm_direct_audit.py` (эта папка) | `metrika_raw_builders.build_check_utm` (реальный) |
|---|---|---|
| Статус в v6_ch | Мёртвый код, 0 вызовов | Активный, вызывается из cron |
| Хранилище | Postgres (`psycopg2`) | ClickHouse |
| Источник | Direct OAuth API + Метрика (live) | `raw_data.metrika_yandex_utm_daily` и др. (raw-слой) |
| Результат | `public.check_utm` (Postgres) | `ad_analytics.check_utm` (ClickHouse) |

## Файлы

| Файл | Описание |
|------|----------|
| `step4.py` | Основной скрипт v6_ch: `CREATE VIEW campaign_status`/`campaign_status_v` из `reference_data.direct_campaigns` |
| `check_utm/utm_direct_audit.py` | v5-код, в v6_ch не вызывается (мёртвый) — см. раздел выше |
| `check_utm/__init__.py` | Пустой |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |
