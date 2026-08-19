# yandex_direct_checking_report

Standalone-модуль для сверки расходов Яндекс.Директа.
С 2026-08-17 тянет данные из ClickHouse `raw_data.yandex_direct_report_rows`, без Reports API v5.

## Зачем

Чтобы проверить «сколько потратил каждый авто-аккаунт по месяцам» по сырью БА6 и сравнить с тем,
что пишет `yandex_direct_manager_reports`. Расхождения обычно из-за разных загрузчиков, фильтров
`direction='Авто'`, исключений по логинам и т.п.

## Что делает

1. Подключается к PostgreSQL (Victory `103.88.240.90`, БД `ad_analytics_bi`)
2. Создаёт таблицу `public.yandex_direct_checking_report` (если её нет) +
   индексы по `account_login` и `month`
3. **TRUNCATE** этой таблицы
4. Читает активные авто-аккаунты в ClickHouse:
   ```sql
   SELECT lowerUTF8(trim(login_key)) AS login, anyLast(domain) AS domain
   FROM reference_data.gsheet_sites
   WHERE status = 'Контекст активно'
     AND direction = 'Авто'
     AND login_key != ''
   GROUP BY login
   ```
5. Агрегирует `raw_data.yandex_direct_report_rows.total_cost` по
   `account_login × manager_login × month` за период `2026-01-01 … вчера`.
   `total_cost` — расход с НДС и комиссией, именно он нужен для отчёта.
6. Пишет строки в таблицу, сверяет с
   `yandex_direct_manager_reports` и шлёт отчёт в Telegram (см. «Сверка и Telegram» ниже).

## Источники данных

| Откуда | Что |
|--------|-----|
| `reference_data.gsheet_sites` | список авто-аккаунтов (login_key + domain) |
| `raw_data.yandex_direct_report_rows` | помесячный `total_cost` с НДС и комиссией |
| `config.tokens` | только Telegram-настройки для отправки отчёта |

Никаких Direct API, `local_yandex`, `big_analytics_*` — источники только ClickHouse
`reference_data.gsheet_sites` и `raw_data.yandex_direct_report_rows`.

## Схема таблицы

```sql
CREATE TABLE IF NOT EXISTS yandex_direct_checking_report (
    id            SERIAL PRIMARY KEY,
    domain        TEXT,
    account_login TEXT NOT NULL,
    manager_login TEXT,
    month         DATE NOT NULL,
    cost          NUMERIC(15,2) NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_yandex_direct_checking_report_login ON yandex_direct_checking_report(account_login);
CREATE INDEX IF NOT EXISTS idx_yandex_direct_checking_report_month ON yandex_direct_checking_report(month);
```

## Сверка и Telegram

После загрузки `run()` вызывает `run_comparison()`: джойнит только что записанные
строки с `public.yandex_direct_manager_reports` (суммы по месяцу через FDW,
`statement_timeout=300000`) и шлёт HTML-отчёт (таблица расхождений > 0.01 ₽ +
список аккаунтов без доступа ни по одному токену) через общий отправитель
`notifications/telegram.py::send_html` (не голый `requests.post`): санитайз,
чанкинг по >4096 символов вместо обрезки текста, ретраи по цепочке прокси,
`timeout=30`. Список failed-аккаунтов передаётся с `collapse_whitespace=False`,
чтобы сохранить отступ `  • login (domain)`.

### Логическая гранулярность

Одна строка = `(account_login, month)`. Кампании внутри аккаунта суммируются в ClickHouse.

## Запуск

### Локально (PC, Windows)
```powershell
cd C:\Users\Mi\PycharmProjects\HomeServer_PythonProject\work\big_analytics_v6_ch
python -m yandex_direct_checking_report.report
```

### На LXC 101 (через Mutagen синкается автоматически)
```bash
ssh ai-agent@192.168.0.202
cd /opt/scripts/work/big_analytics_v6_ch
~/venv-v6/bin/python3 -m yandex_direct_checking_report.report
```

### На Victory VPS (нужен scp вручную)
```bash
scp -r yandex_direct_checking_report victory:~/big_analytics_v6_ch/
ssh victory
cd ~/big_analytics_v6_ch
~/venv-v6/bin/python3 -m yandex_direct_checking_report.report
```

## Логи

Пишутся в stdout:
```
2026-05-22 14:00:01 [INFO] Период: 2026-01-01 … 2026-05-21
2026-08-17 15:10:01 [INFO] Период: 2026-01-01 … 2026-08-16
2026-08-17 15:10:08 [INFO] ClickHouse raw_data: active_accounts=267, accounts_with_cost=261, rows=865
2026-08-17 15:10:09 [INFO] Готово: rows=865, accounts_with_cost=261, no_data=6
```

## Параметры

Все настройки в `report.py`:

| константа | значение | смысл |
|-----------|----------|-------|
| `TABLE_NAME` | `'yandex_direct_checking_report'` | имя таблицы |
| `DATE_FROM` | `'2026-01-01'` | начало периода |

## Проверки качества

После запуска:

```sql
-- Сколько аккаунтов и месяцев
SELECT COUNT(DISTINCT account_login) AS accounts,
       COUNT(DISTINCT month) AS months,
       COUNT(*) AS rows,
       SUM(cost) AS total_cost_rub
FROM yandex_direct_checking_report;

-- Аккаунты без manager_login (ни один токен не сработал)
SELECT account_login, domain
FROM yandex_direct_checking_report
WHERE manager_login IS NULL;

-- Сравнение с big_analytics_full
SELECT cr.month, cr.account_login, cr.cost AS cost_api,
       baf.spend AS cost_baf,
       cr.cost - baf.spend AS diff
FROM yandex_direct_checking_report cr
LEFT JOIN (
    SELECT date_trunc('month', date)::date AS month,
           account_login,
           SUM(spend) AS spend
    FROM big_analytics_full
    WHERE direction = 'Авто'
    GROUP BY 1, 2
) baf USING (month, account_login)
WHERE ABS(COALESCE(cr.cost, 0) - COALESCE(baf.spend, 0)) > 1
ORDER BY ABS(cr.cost - COALESCE(baf.spend, 0)) DESC
LIMIT 50;
```

## Ограничения и нюансы

1. **`total_cost` обязателен.** Семён подтвердил: это расход с НДС и комиссией, то есть нужная
   сумма для отчёта. Не заменять на `cost`.
2. **Аккаунт без расхода** не получает строку в БД, учитывается в `no_data`.
3. **Не входит в pipeline.py**. Это отдельный отчёт, запускается руками или
   по отдельному крону (если потребуется регулярно).
4. **Telegram-сверка** всё ещё сравнивает с `yandex_direct_manager_reports`; это старый FDW-источник,
   поэтому в нём сохранён `statement_timeout=300000`.

## Файлы модуля

```
yandex_direct_checking_report/
├── report.py     — основной скрипт
├── CLAUDE.md     — короткая инструкция для ИИ
└── README.md     — этот файл
```
