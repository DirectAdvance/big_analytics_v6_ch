"""
settings.py — константы конфигурации big_analytics_v5
"""
import sys
from pathlib import Path

for _p in Path(__file__).resolve().parents:
    if (_p / '.secret' / 'loader.py').exists():
        sys.path.insert(0, str(_p / '.secret'))
        break
from loader import load_db  # noqa: E402

# ── Подключение к БД ──────────────────────────────────────────────────────────
# TCP_KEEPALIVES_2026-07-27: включаем keepalives на всех соединениях к Victory.
# Без keepalives провайдерский firewall режет idle TCP-соединение после ~600 сек
# (INSERT big_analytics_direct длится 10+ мин без трафика → "connection already
# closed" → PG crash recovery → UNLOGGED tables wiped → PREFLIGHT FAIL).
# keepalives_idle=60 → первый probe через 60 сек silence; interval=10; count=5.
_KA = {'keepalives': 1, 'keepalives_idle': 60, 'keepalives_interval': 10, 'keepalives_count': 5}
_DB = {**load_db('victory'), **_KA}
DB_SRC = {**_DB, 'database': 'ad_analytics'}    # источник: только чтение
DB_DST = {**_DB, 'database': 'ad_analytics_bi'} # результат: запись

_DB_LOCAL = load_db('local')
DB_LOCAL_SRC = {**_DB_LOCAL, 'database': 'big_analytics'}  # локальный big_analytics для copy_metrika

# Обратная совместимость (шаги 1–6 используют conn к ad_analytics_bi)
DB = DB_DST

# ── Диапазон данных ───────────────────────────────────────────────────────────
DATE_FROM = '2026-01-01'          # первый запуск: копируем с этой даты

# ── Инкрементальная синхронизация ─────────────────────────────────────────────
SAFETY_DAYS   = 3                 # перекрываем N дней назад по updated_at
BATCH_DAYS    = 7                 # размер батча при чтении из источника (дней)
STREAM_CHUNK  = 20_000            # server-side cursor: строк за раз
BATCH_RETRIES = 5                 # попыток при сбое батча
BATCH_RETRY_PAUSE = 10            # пауза между попытками (сек)

# ── Исключения ────────────────────────────────────────────────────────────────
# ⚠️ ЛЕГАСИ, НЕ ДЛЯ КОНТУРА CLICKHOUSE: это PG-копия под v5-нумерацию `domain_id` (буквально
# скопирована при форке из work/big_analytics_v5/config/settings.py). Числовой domain_id
# НЕ переносится между PostgreSQL (v5) и ClickHouse (v6) — своя нумерация в каждой системе
# (см. KNOWN_ISSUES.md #33). Актуальный фильтр ClickHouse-контура — ИМЕНА доменов,
# `config/ch_settings.py::EXCLUDED_DOMAIN_NAMES`, используется `step1_load_raw/step1.py`.
# У этой константы (`EXCLUDED_DOMAIN_IDS`) на 2026-08-06 нет активных потребителей в v6_ch —
# не переиспользовать её вслепую для ClickHouse-фильтрации.
EXCLUDED_DOMAIN_IDS = (1645, 883)  # 1645=priezd shared key3; 883=victory-crm.ru (не клиент)

# ── PostgreSQL оптимизация ────────────────────────────────────────────────────
WORK_MEM = '1999MB'               # SET work_mem перед тяжёлыми JOIN (шаги 3–4)

# CDR_SPLIT_2026-07-27: SQL-паттерн CDR — единая константа для step3 + step13.
# Расширен относительно исходного LIKE '%subsource:cdr%': захватывает также
# формат cdr_<телефон> (1 576 лидов июня 2026). ~* = case-insensitive POSIX regex.
# Граница токена ([^a-z0-9]) исключает будущие ложные срабатывания на подстроку.
CDR_PATTERN = r"(^|[^a-z0-9])cdr([^a-z0-9]|$)"

# ── Имена таблиц ─────────────────────────────────────────────────────────────

# Локальные копии (живут между запусками, в ad_analytics_bi)
# Соглашение: local_<имя> (префикс, а не суффикс)
T_YANDEX_LOCAL          = 'local_yandex'
T_DOMAINS_LOCAL         = 'local_domains'
T_GSHEET_SITES          = 'local_gsheet_sites'
T_GSHEET_NAMING         = 'local_gsheet_naming'
T_GSHEET_PLAN_FAKT      = 'local_gsheet_plan_fakt'
T_GSHEET_AUTOSALONY     = 'local_gsheet_autosalony_clients'
T_GSHEET_PRIEZDI        = 'local_gsheet_priezdi_marcar'
T_CRM_STATUSES          = 'local_crm_statuses'

# Конфигурация
T_NAME_REPLACEMENTS     = 'name_replacements'
T_DATA_QUALITY_LOG      = 'data_quality_log'
T_DIRECT_HISTORY        = 'yandex_direct_history'

# RAW UNLOGGED (пересоздаются каждый запуск)
T_RAW_YANDEX            = 'raw_yandex'
T_RAW_LEADS             = 'raw_leads'
T_RAW_CALLS             = 'raw_calls'
T_RAW_DOMAINS           = 'raw_domains'
T_RAW_PERFORM_LEADS     = 'raw_perform_leads'   # PERFORM_LEADS_2026-07-01

# Результирующие таблицы
T_DIRECT                = 'big_analytics_direct'
T_REVIEWS               = 'big_analytics_reviews'
T_CROP                  = 'big_analytics_crop_targeting'
T_SEO                   = 'big_analytics_seo'
T_PIXEL                 = 'big_analytics_pixel'
T_TELEGRAM              = 'big_analytics_telegram'
T_FULL                  = 'big_analytics_full'
T_FULL_ARRIVAL          = 'big_analytics_full_arrival'

# Источники в ad_analytics (только имена таблиц, schema=public)
SRC_LEADS               = 'leads'
SRC_YANDEX              = 'yandex_direct_manager_reports'
SRC_DOMAINS             = 'domains'
SRC_GSHEET_SITES        = 'gsheet_sites'
SRC_GSHEET_NAMING       = 'gsheet_naming'
SRC_GSHEET_PLAN_FAKT    = 'gsheet_plan_fakt'
SRC_GSHEET_AUTOSALONY   = 'gsheet_autosalony_clients'
SRC_GSHEET_PRIEZDI      = 'gsheet_priezdi_marcar'
SRC_CRM_STATUSES        = 'crm_statuses'
SRC_LEADS_ALL           = 'leads_all'
SRC_PERFORM_LEADS       = 'perform_leads'   # PERFORM_LEADS_2026-07-01

# Локальная копия leads_all + perform_leads
T_LEADS_ALL_LOCAL         = 'local_leads_all'
T_LOCAL_PERFORM_LEADS     = 'local_perform_leads'     # PERFORM_LEADS_2026-07-01

# T_LEADS_CROP_ATTR_2026-06-17: таблица атрибуции лидов к посевам (создаётся step3,
# заполняется внешним скриптом). Использовалась в step3 — JOIN для фильтрации посевов из direct.
T_LEADS_CROP_ATTR       = 'leads_crop_attribution'

# VK_ADS_INTEGRATION_2026-07-06: локальная копия расходов ВК Реклама (pre-агрег. day+account+ad_plan)
T_LOCAL_VK_ADS          = 'local_vk_ads_stats_day'

# ── Блоки минус-снапшота (step14, ночной пайплайн) ───────────────────────────
# Список блок-фильтров для step14_minus_snapshot/step14.py.
# Каждый элемент передаётся как --block при запуске из pipeline_night.py.
MINUS_SNAPSHOT_BLOCKS = ['tp2', 'tp4']

# Полный список TRUNCATE+INSERT таблиц (src → local, полная замена)
TRUNCATE_INSERT_TABLES = [
    (SRC_DOMAINS,            T_DOMAINS_LOCAL),
    (SRC_GSHEET_SITES,       T_GSHEET_SITES),
    (SRC_GSHEET_NAMING,      T_GSHEET_NAMING),
    (SRC_GSHEET_PLAN_FAKT,   T_GSHEET_PLAN_FAKT),
    (SRC_GSHEET_AUTOSALONY,  T_GSHEET_AUTOSALONY),
    (SRC_GSHEET_PRIEZDI,     T_GSHEET_PRIEZDI),
    (SRC_CRM_STATUSES,       T_CRM_STATUSES),
    (SRC_LEADS_ALL,          T_LEADS_ALL_LOCAL),
    (SRC_PERFORM_LEADS,      T_LOCAL_PERFORM_LEADS),   # PERFORM_LEADS_2026-07-01
]
