# yandex_direct_checking_report

Standalone-модуль для сверки расходов Яндекс.Директа.
Тянет данные напрямую из Reports API v5, без посредников типа `yandex_direct_manager_reports`.

## Зачем

Чтобы независимо проверить «сколько на самом деле потратил каждый авто-аккаунт по месяцам»
и сравнить с тем что пишет `big_analytics_full` / `local_yandex`. Расхождения
в нашем пайплайне обычно из-за кэшей менеджерских отчётов, фильтров `direction='Авто'`,
исключений по логинам и т.п. — этот модуль даёт «правду от Яндекса» как точку отсчёта.

## Что делает

1. Подключается к PostgreSQL (Victory `103.88.240.90`, БД `ad_analytics_bi`)
2. Создаёт таблицу `public.yandex_direct_checking_report` (если её нет) +
   индексы по `account_login` и `month`
3. **TRUNCATE** этой таблицы
4. Читает активные авто-аккаунты:
   ```sql
   SELECT DISTINCT ON (login_key) login_key, domain
   FROM public.local_gsheet_sites
   WHERE status = 'Контекст активно'
     AND direction = 'Авто'
     AND login_key IS NOT NULL AND TRIM(login_key) <> ''
   ORDER BY login_key, domain
   ```
5. Для каждого `login_key` запрашивает Reports API v5:
   - `ReportType = CAMPAIGN_PERFORMANCE_REPORT`
   - `FieldNames = ['Month', 'Cost']`
   - `DateRangeType = CUSTOM_DATE`
   - `DateFrom = 2026-01-01`, `DateTo = вчера`
   - Заголовки: `IncludeVAT: NO`, `returnMoneyInMicros: false`
6. Перебирает 4 агентских OAuth-токена. Первый давший `HTTP 200` →
   `manager_login` = логин этого агентского аккаунта.
7. Парсит TSV, считает суммы по месяцам, пишет в таблицу.

## Источники данных

| Откуда | Что |
|--------|-----|
| `public.local_gsheet_sites` | список авто-аккаунтов (login_key + domain) |
| Reports API v5 (`api.direct.yandex.com/json/v5/reports`) | помесячный Cost без НДС |
| `.secret/.env` через `config.tokens` | 4 OAuth-токена |

Никаких `raw_*`, `local_yandex`, `big_analytics_*` — независимо.

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

### Логическая гранулярность

Одна строка = `(account_login, month)`. Несколько кампаний внутри аккаунта
суммируются на стороне Direct API (мы запрашиваем только `Month` + `Cost`).

## Запуск

### Локально (PC, Windows)
```powershell
cd C:\Users\Mi\PycharmProjects\HomeServer_PythonProject\work\big_analytics_v5
python -m yandex_direct_checking_report.report
```

### На LXC 101 (через Mutagen синкается автоматически)
```bash
ssh ai-agent@192.168.0.202
cd /opt/scripts/work/big_analytics_v5
~/venv/bin/python3 -m yandex_direct_checking_report.report
```

### На Victory VPS (нужен scp вручную)
```bash
scp -r yandex_direct_checking_report victory:/home/<user>/big_analytics_v5/
ssh victory
cd ~/big_analytics_v5
~/venv/bin/python3 -m yandex_direct_checking_report.report
```

## Логи

Пишутся в stdout:
```
2026-05-22 14:00:01 [INFO] Период: 2026-01-01 … 2026-05-21
2026-05-22 14:00:01 [INFO] Активных аккаунтов (Контекст активно, Авто): 234
2026-05-22 14:00:02 [INFO] [1/234] avtomir-msk (avtomir.ru) ...
2026-05-22 14:00:05 [INFO]   → 5 месяцев, итого 145820.34 ₽ (mgr=victorylotsofads1)
…
2026-05-22 14:42:11 [INFO] Готово: rows=1041, accounts ok=212, no_data=15, failed=7
```

## Параметры

Все настройки в `report.py`:

| константа | значение | смысл |
|-----------|----------|-------|
| `TABLE_NAME` | `'yandex_direct_checking_report'` | имя таблицы |
| `DATE_FROM` | `'2026-01-01'` | начало периода |
| `TOKENS` | 4 пары `(token, manager_login)` | приоритет перебора |
| `MAX_RETRY_HTTP5XX` | `5` | повторы при 500/502/503 |
| `PAUSE_BETWEEN_LOGINS` | `0.4` | пауза между аккаунтами, сек |

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

1. **Лимиты Direct API**. Каждый аккаунт = 1 запрос. Если активных ~300 — это
   ~10 минут с паузами и retry на 201/202. Параллелизма нет (последовательно).
2. **Аккаунт без активных кампаний**. API вернёт пустой TSV — `monthly={}`,
   запись в БД не пишется (counter `no_data_accounts`).
3. **400/401/403**. Считаются как «этим токеном нет доступа» → перебор.
   Если все 4 дают 4xx → `failed_accounts++`, строка не пишется.
4. **Не входит в pipeline.py**. Это отдельный отчёт, запускается руками или
   по отдельному крону (если потребуется регулярно).
5. **`returnMoneyInMicros=false`** — Cost приходит в рублях float, делить
   на 1_000_000 НЕ нужно.
6. **`IncludeVAT=NO`** — расход без НДС. Это стандарт всех агрегаций
   в `big_analytics_v5`.

## Файлы модуля

```
yandex_direct_checking_report/
├── report.py     — основной скрипт
├── CLAUDE.md     — короткая инструкция для ИИ
└── README.md     — этот файл
```
