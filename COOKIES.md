# COOKIES.md — куки Яндекс.Директ

> Всё про куки: автообновление с glavpotok, проверка живости, параллельный prefetch
> (step4/step9), нюанс `manager_login`. Вынесено из `CLAUDE.md` (lazy-load).

---

## Автообновление куки с glavpotok (основной способ, июнь 2026)

Куки обновляются **автоматически** с эндпоинта glavpotok.ru — ручное обновление больше не нужно.

**Скрипт:** `refresh_cookies.py` (в корне `big_analytics_v5/`, рядом с `pipeline.py`).
- Функция `refresh_cookies() -> dict`: для каждого из 3 менеджерских логинов
  (`victorylotsofads1`, `victoryagency-direct1618440`, `victoryagency14`) делает
  `GET https://glavpotok.ru/api/cookies/yandex-direct/<login>` с заголовком
  `Authorization: Bearer <token>`, собирает `{login: cookie_string}` и **атомарно**
  перезаписывает `cookies.json`. Таймаут 20с, retry до 3 раз.
- `cookie_string` совместима 1:1 с HTTP-заголовком `Cookie` — парсинг не нужен.
- Поведение по логину: `200` → берём; `404` → варнинг, оставляем старую куку из текущего
  `cookies.json` (не падаем); `401` → `RuntimeError` (нет/неверный токен); сеть/5xx → retry,
  затем как 404.
- Standalone: `~/venv/bin/python3 refresh_cookies.py`.

**Токен** — секрет, в `.secret/.env` → `GLAVPOTOK_COOKIES_TOKEN` (+ `GLAVPOTOK_COOKIES_URL`),
читается через `loader.py:load_glavpotok_cookies()`. На Victory — в `~/.secret/.env`.
**Никогда не хардкодить токен в .py и не коммитить.**

**Где врезано:**
- `pipeline.py` (начало `main()`): `refresh_cookies()` вызывается ДО проверки живости кук.
- `step_cron_night/pipeline_night.py` (`main()`): `_refresh_cookies()` перед всеми ночными шагами.
- **Self-healing** в `pipeline.py`: если `check_all_cookies_strict` нашёл мёртвые куки —
  принудительный повторный `refresh_cookies()` + перепроверка. Только если и после рефреша
  мертво → Telegram + `sys.exit(1)` (текст: «Не удалось обновить автоматически с glavpotok»).

### Деплой refresh_cookies на Victory

```bash
scp work/big_analytics_v5/refresh_cookies.py victory:~/big_analytics_v5/
scp .secret/loader.py victory:~/.secret/loader.py   # содержит load_glavpotok_cookies()
# + добавить GLAVPOTOK_COOKIES_TOKEN/URL в ~/.secret/.env на Victory
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 refresh_cookies.py"  # проверка
```

> SSH alias `victory` может отсутствовать локально (publickey denied) — тогда деплой по паролю:
> `LOGIN`/`PASS` в `.secret/.env` (`ssh semen_vi@103.88.240.90`), через `expect`/`scp`.

---

## (Устарело) Ручное обновление куки с хоум-сервера

Старый способ (оставлен как fallback, шлюз ещё работает):
```bash
curl -s -H "X-API-Key: <GATEWAY_API_KEY>" http://192.168.0.202:8765/cookies -o /tmp/cookies.json
scp /tmp/cookies.json victory:~/big_analytics_v5/cookies.json
```
Актуальный ключ — в `HomeServer_PythonProject/.secret/.env` → `COOKIES_API_KEY`.

---

## Проверка куки перед шагами (config/cookies.py)

Файл `config/cookies.py` — общий модуль проверки куки:
- `check_cookies_alive(cookies)` — тестирует Grid API через 2 известных аккаунта
- `send_tg(text)` — отправляет сообщение в Telegram
- `send_tg_cookies_dead(step_name)` — уведомление о протухших куках

**Поведение при протухших куках:**
- **step7** (статусы кампаний): `_build_sessions()` возвращает `[]` → TG + return, старые данные сохраняются
- **step9** (история Директа): `check_cookies_alive()` после загрузки → TG + return

Тестовые аккаунты (`_SESSION_TEST_LOGINS` в step7, `_TEST_LOGINS` в cookies.py):
- `victorylotsofads1` → `e-20074351`
- `victoryagency-direct1618440` → `acbu-spb-436222-ns89`
- `victoryagency14` → `porg-7uhutcdh`

---

## ⚠️ manager_login в local_yandex содержит @yandex.ru суффикс

Поле `manager_login` в `local_yandex` хранится как `victorylotsofads1@yandex.ru`, но ключи в `cookies.json` — без суффикса (`victorylotsofads1`). При группировке по manager_login обязательно обрезать:

```python
mgr = (manager_login or 'unknown').split('@')[0]
```

Применено в:
- `step4_campaign_status/step4.py` — `prefetch_statuses()`
- `step9_direct_history/step9.py` — `_get_active_logins()`

**Симптом нарушения:** все аккаунты попадают в `failed_accounts` → retry последовательным перебором вместо 3 параллельных потоков.

---

## Параллельный prefetch: архитектура (step7 и step9)

Оба шага используют паттерн: 1 поток = 1 куки = 1 manager_login.

**step7** (`prefetch_statuses`):
- Группирует аккаунты по `manager_login` → 3 группы
- Валидирует каждую куку отдельно через `_build_sessions({name: cookie})`
- 3 потока параллельно, `INSERT ON CONFLICT DO UPDATE` для безопасной записи
- Аккаунты с `manager_login='unknown'` или без куки → `failed_accounts` → retry перебором всех сессий

**step9** (`prefetch_history`):
- Аналогичная схема: `_get_active_logins()` возвращает `(login, manager_login)` туплы
- 3 потока через `_history_worker`, провалившиеся → retry перебором

**401 при retry** — ожидаемо: неправильная куки пробует чужой аккаунт → `None` → следующая сессия. Уровень WARNING, не ERROR.

### Порядок фоновых потоков в pipeline.py

| Момент запуска | Поток | Ждёт |
|----------------|-------|-------|
| После step0 | `prefetch_statuses` (step7) | step4 через `prefetch_thread.join()` |
| После step4 | `prefetch_history` (step9) | step9 через `history_thread.join()` |

step9 стартует **после** завершения step7 prefetch — исключает CSRF-конфликты при одновременном использовании одних куков.

### Telegram по завершению step7 prefetch

После окончания prefetch_statuses() приходит сообщение вида:
```
step7 статусы кампаний
Готово: 1288 статусов, 133 ошибок
  Активна: 1065
  Архив: 20
  Остановлена: 203
```

### Невалидные login_key в local_gsheet_sites

В step9 (`_get_active_logins`) добавлен фильтр: логины с не-ASCII символами (например, `'Нет'`)
пропускаются с предупреждением. Это предотвращает `latin-1` ошибку в HTTP-заголовке `Referer`.
