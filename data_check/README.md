# data_check — подсистема проверок качества данных

> Автономный агент контроля целостности витрины `big_analytics_full` и справочника
> `local_gsheet_sites`. Сверяет данные БД с эталонной Google-таблицей проджектов,
> проверяет инварианты воронки, расходы без лидов и свежесть пайплайна.
> Результат — человекочитаемый отчёт (stdout / Telegram) + exit-code для cron/CI.

---

## Запуск

```bash
cd big_analytics_v5
~/venv/bin/python3 data_check/run.py            # полный отчёт в stdout
~/venv/bin/python3 data_check/run.py --json     # JSON в stdout
~/venv/bin/python3 data_check/run.py --tg        # + отправить отчёт в Telegram
```

**Exit codes:** `0` = OK, `1` = найдены критичные проблемы, `2` = ошибка скрипта.

«Критично» (даёт exit 1): домены только в Sheets, домены без данных в `big_analytics_full`,
расход без лидов за 7 дней, нарушения инвариантов воронки.

---

## Архитектура

```
run.py  ── точка входа: читает Google Sheets → гоняет все чеки → reporter
  ├─ sheets_reader.read_sheet()      → эталонный список проджектов из Google Sheets
  ├─ checks/projects.run()           → расхождения Sheets ↔ БД
  ├─ checks/fields.run()             → NULL в ключевых полях local_gsheet_sites
  ├─ checks/spending.run()           → расходы vs лиды (7 / 30 дней)
  ├─ checks/funnel.run()             → инварианты воронки по проджектам
  ├─ _check_freshness()              → давность последнего успешного step8
  └─ reporter.format_report() / send_telegram()
```

Подключение к БД — `config.settings.DB_DST` (`ad_analytics_bi`). Telegram — токены из
`config.tokens` (через `.secret/.env` + `loader.py`).

---

## Что проверяет каждый чек

### `checks/projects.py` — домены Sheets ↔ БД
Сверяет домены из эталонной Google-таблицы (`SPREADSHEET_ID = 1wMAfpMyHEwa99NT0-…`,
`GID = 1519720357`) с `local_gsheet_sites` и наличием данных в `big_analytics_full`.
Возвращает:
- `only_in_sheets` — есть в Sheets, нет в БД (**критично**);
- `only_in_db` — есть в БД, нет в Sheets (предупреждение);
- `no_analytics_data` — есть в обоих, но 0 строк в `big_analytics_full` (**критично**).

Имя колонки-домена ищется без учёта регистра (`сайт`/`домен`/`domain`/`site`/`url`/`адрес`).

### `checks/fields.py` — NULL в `local_gsheet_sites`
Находит домены с незаполненными ключевыми полями (предупреждения):
- `null_directologist` — пустой `directologist`;
- `null_manager_login` — пустой `project_manager`;
- `null_login_key` — пустой `login_key` или значение `'Нет'`.

### `checks/spending.py` — расход vs лиды (7 / 30 дней)
Агрегирует `big_analytics_full` по проджекту:
- `spend_no_leads_7d` — есть расход за 7 дней, но **0 лидов** (**критично** — деньги тратятся впустую);
- `no_spend_7d` — нет расходов за 7 дней (предупреждение);
- `per_project` — полная матрица cost/leads за 7 и 30 дней.

### `checks/funnel.py` — инварианты воронки
По каждому проджекту за 30 дней проверяет вложенность воронки (`check_invariants`):
`kval ≤ korr`, `priezd ≤ kval`, `prodazhi ≤ priezd`, нет продажи без визита,
`dobro ≤ dohod_do_kredita`. Возвращает:
- `invariant_violations` — нарушения (**критично**);
- `zero_funnel_active` — проджекты с нулевой воронкой за 30 дней (предупреждение);
- `per_project` — все метрики воронки по проджектам.

### `_check_freshness()` (в `run.py`) — свежесть пайплайна
Берёт `MAX(run_at)` из `data_quality_log` где `step='step8' AND status='ok'`.
Если последний успешный прогон был **>24 ч назад** → флаг `stale` (предупреждение
«PBI устарел»).

---

## Отчёт (`reporter.py`)

`format_report()` собирает результаты в два блока:
- **❌ КРИТИЧНО** — расхождения проджектов, расход без лидов, нарушения воронки;
- **⚠️ ПРЕДУПРЕЖДЕНИЯ** — NULL-поля, нет расходов 7д, нулевая воронка, устаревший pipeline.

`send_telegram()` шлёт отчёт в чат (chunk-split по 4096 символов), при `--tg`.
Прокси/токен/chat_id — из `config.tokens`.

---

## Связи

- **Источник правды по проджектам:** Google Sheets `1wMAfpMyHEwa99NT0-…` (gid `1519720357`).
- **Проверяемые таблицы:** `big_analytics_full`, `local_gsheet_sites`, `data_quality_log`.
- **Инварианты воронки** — те же, что в [`FUNNEL.md`](../FUNNEL.md) и
  [`PROJECT_CHARTER.md`](../PROJECT_CHARTER.md) §5.
- **Не путать** с `crm_mappings_check/` (целостность `local_crm_statuses`) и
  `step12_proverka_big_analytics/` (проверки внутри пайплайна).
