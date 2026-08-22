# COOKIES.md — куки Яндекс.Директ

> Всё про куки: автообновление с glavpotok, проверка живости, единственный потребитель кук
> в v6_ch (step139 `direct_placement_links`), нюанс `manager_login`. Вынесено из `CLAUDE.md`
> (lazy-load).

---

## Автообновление куки с glavpotok (основной способ, июнь 2026)

Куки обновляются **автоматически** с эндпоинта glavpotok.ru — ручное обновление больше не нужно.

**Скрипт:** `refresh_cookies.py` (в корне `big_analytics_v6_ch/`, рядом с `pipeline.py`).
- Функция `refresh_cookies() -> dict`: для каждого из 3 менеджерских логинов
  (`victorylotsofads1`, `victoryagency-direct1618440`, `victoryagency14`) делает
  `GET https://glavpotok.ru/api/cookies/yandex-direct/<login>` с заголовком
  `Authorization: Bearer <token>`, собирает `{login: cookie_string}` и **атомарно**
  перезаписывает `cookies.json`. Таймаут 20с, retry до 3 раз.
- `cookie_string` совместима 1:1 с HTTP-заголовком `Cookie` — парсинг не нужен.
- Поведение по логину: `200` → берём; `404` → варнинг, оставляем старую куку из текущего
  `cookies.json` (не падаем); `401` → `RuntimeError` (нет/неверный токен); сеть/5xx → retry,
  затем как 404.
- Standalone: `~/venv-v6/bin/python3 refresh_cookies.py`.

**Токен** — секрет, в `.secret/.env` → `GLAVPOTOK_COOKIES_TOKEN` (+ `GLAVPOTOK_COOKIES_URL`),
читается через `loader.py:load_glavpotok_cookies()`. На Victory — в `~/.secret/.env`.
**Никогда не хардкодить токен в .py и не коммитить.**

**Где врезано (v6_ch):** `pipeline.py` сам НЕ вызывает `refresh_cookies()` и не проверяет живость
кук на старте — куки нужны только шагу 139 `direct_placement_links` (Grid API для ссылок площадок);
step4/step9 читают `reference_data.direct_campaigns` из ClickHouse и кук не касаются.
- Единая точка входа — `config/cookies.py::ensure_cookies_alive_or_stop()`, вызывается из
  `direct_placement_links/build.py::build()` перед запросами к Grid API.
- **Self-healing**: если `check_all_cookies_strict` нашёл мёртвые куки — `ensure_cookies_alive_or_stop()`
  сам делает `refresh_cookies()` + перепроверку. Если и после рефреша мертво → Telegram +
  `CookiesDeadError` (шаг ловит исключение и делает clean-exit, не `sys.exit(1)`).

### Деплой refresh_cookies на Victory

```bash
scp work/big_analytics_v6_ch/refresh_cookies.py victory:~/big_analytics_v6_ch/
scp .secret/loader.py victory:~/.secret/loader.py   # содержит load_glavpotok_cookies()
# + добавить GLAVPOTOK_COOKIES_TOKEN/URL в ~/.secret/.env на Victory
ssh victory "cd ~/big_analytics_v6_ch && ~/venv-v6/bin/python3 refresh_cookies.py"  # проверка
```

> SSH alias `victory` может отсутствовать локально (publickey denied) — тогда деплой по паролю:
> `LOGIN`/`PASS` в `.secret/.env` (`ssh semen_vi@103.88.240.90`), через `expect`/`scp`.

---

## (Устарело) Ручное обновление куки с хоум-сервера

Старый способ (оставлен как fallback, шлюз ещё работает):
```bash
curl -s -H "X-API-Key: <GATEWAY_API_KEY>" http://192.168.0.202:8765/cookies -o /tmp/cookies.json
scp /tmp/cookies.json victory:~/big_analytics_v6_ch/cookies.json
```
Актуальный ключ — в `HomeServer_PythonProject/.secret/.env` → `COOKIES_API_KEY`.

---

## Проверка куки перед шагами (config/cookies.py)

Файл `config/cookies.py` — общий модуль проверки куки:
- `check_cookies_alive(cookies)` — тестирует Grid API через 2 известных аккаунта
- `send_tg(text)` — отправляет сообщение в Telegram (тонкая обёртка над
  `notifications.telegram.send_html`, с 2026-08-14)

**Поведение при протухших куках (v6_ch):**
- **step139** `direct_placement_links` — единственный потребитель живых кук; `ensure_cookies_alive_or_stop()`
  останавливает шаг через `CookiesDeadError`, если куки мертвы и после рефреша.
- step4/step9 куки больше не читают (данные из `reference_data.direct_campaigns`) — v5-архитектура
  с `_build_sessions()`/`check_cookies_alive()`-после-загрузки в них не применяется.

Тестовые аккаунты (`_TEST_LOGINS` в `config/cookies.py`):
- `victorylotsofads1` → `e-20074351`
- `victoryagency-direct1618440` → `acbu-spb-436222-ns89`
- `victoryagency14` → `porg-7uhutcdh`

---

## ⚠️ manager_login в local_yandex содержит @yandex.ru суффикс

Поле `manager_login` в `local_yandex` хранится как `victorylotsofads1@yandex.ru`, но ключи в `cookies.json` — без суффикса (`victorylotsofads1`). При группировке по manager_login обязательно обрезать:

```python
mgr = (manager_login or 'unknown').split('@')[0]
```

Применено в (v6_ch):
- `direct_placement_links/build.py` — `replaceRegexpOne(any(manager_login), '@yandex\\.ru$', '')` (SQL).
- step4/step9 больше не группируют по кукам/`manager_login` (v5-наследие, см. ниже).

**Симптом нарушения (v6_ch):** `direct_placement_links` не матчит `manager_login` на аккаунт из
`cookies.json` → куки для этого логина не находятся. v5-симптом «все в `failed_accounts` → retry
перебором» относился к удалённой в v6 threaded-архитектуре ниже.

---

## (Устарело в v6_ch) Параллельный cookie-prefetch step7/step9

v5-архитектура ниже (1 поток = 1 куки = 1 `manager_login`, `_build_sessions`, `_get_active_logins`,
`_history_worker`, фоновые `prefetch_thread`/`history_thread` в `pipeline.py`, Telegram-отчёт
«step7 статусы кампаний») в v6_ch отсутствует: `step4_campaign_status/step4.py::prefetch_statuses()`
и `step9_direct_history/step9.py::prefetch_history()` — no-op заглушки для обратной совместимости
сигнатур, `run()` обоих шагов строит вьюхи из `reference_data.direct_campaigns` без единого
похода в Grid API. Кук эти шаги не читают, `failed_accounts`/CSRF-retry/401-warning из v5 к ним
не относятся. Живой потребитель кук в v6_ch — только step139 `direct_placement_links`
(см. «Где врезано» выше). Функций `_build_sessions`, `_get_active_logins`, `_history_worker`,
`_SESSION_TEST_LOGINS` в кодовой базе v6_ch нет.
