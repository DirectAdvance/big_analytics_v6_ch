"""Step 1 for v6_ch: build RAW tables in ClickHouse from raw_data.

This replaces the v5 Postgres path (`local_*` -> UNLOGGED raw_*). The v6 raw
tables are rebuilt in `ad_analytics` from the existing `raw_data` schema. Direct
and CRM sources can be overridden by PG-backed local copies when raw_data is
known to be behind or lossy for historical periods.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM, EXCLUDED_DOMAIN_NAMES, RAW_TARGET_TABLES
from config.ch_utils import (
    SAFE_QUERY_SETTINGS,
    apply_storage_codecs,
    count_rows,
    day_ranges,
    replace_view,
    swap_shadow,
    table_exists,
)

logger = logging.getLogger("pipeline.step1")

DIRECT_TOTAL_COST_FACTOR_OVERRIDES = (
    {
        "account_login": "porg-kkhtgf2u",
        "date_from": "2026-01-01",
        "date_to": "2026-01-28",
        "multiplier": "10",
        "divisor": "7",
    },
    {
        "account_login": "e-20086619",
        "domain": "samara-buavto.ru",
        "date_from": "2026-03-02",
        "date_to": "2026-03-03",
        "factor": "1.4022552",
        "require_equal_cost": False,
    },
)


def _excluded_domain_names_sql() -> str:
    """SQL-литерал списка исключённых доменов ПО ИМЕНИ (не по числовому id — id непереносим
    между PostgreSQL v5 и ClickHouse v6, см. EXCLUDED_DOMAIN_NAMES_2026-08-06 в config/ch_settings.py).
    Экранирует одинарную кавычку в имени домена — сейчас EXCLUDED_DOMAIN_NAMES это внутренняя
    константа без внешнего входа (эксплуатации нет), но экранирование дешевле, чем разбираться
    с битым SQL, если список когда-нибудь станет конфигурируемым."""
    return ", ".join("'{}'".format(name.lower().replace("'", "''")) for name in EXCLUDED_DOMAIN_NAMES)


def _excluded_domain_filter_sql(domain_expr: str) -> str:
    """WHERE-условие фильтра по EXCLUDED_DOMAIN_NAMES для указанного SQL-выражения домена.

    Пустой EXCLUDED_DOMAIN_NAMES → возвращает пустую строку (условие не подставляется), а НЕ
    `NOT IN ()` — в ClickHouse это синтаксическая ошибка. "Опустошить список" — естественный
    способ временно отключить фильтр, он не должен ронять step1 (EXCLUDED_DOMAIN_NAMES_GUARD_2026-08-06).
    """
    if not EXCLUDED_DOMAIN_NAMES:
        return ""
    return (
        f"  AND lowerUTF8(trim(ifNull({domain_expr}, ''))) NOT IN ({_excluded_domain_names_sql()})\n"
    )


# ══════════════════════════════════════════════════════════════════════════════
# MARCAR_GSHEET_STATUS_2026-08-05 — патч статусов Маркара по гугл-таблице приездов
# ------------------------------------------------------------------------------
# Порт v5 `step0_sync_local/step0.py::_patch_marcar_statuses()` (v5:1228, вызов v5:1741).
# В v5 патч был UPDATE по локальной копии `local_leads_all`; в v6 источник
# `raw_data.leads_all` — реплика CRM, писать в неё нельзя, поэтому патч сдвинут на
# один шаг вниз: он применяется ВЫРАЖЕНИЕМ при сборке `raw_leads` / `raw_calls`.
# Результат для всех последующих шагов идентичен v5 — status у лида Маркара уже
# патченый ещё до step3 (`_step3_leads_deduped`), до дедупликации и до воронки.
#
# Механика: в `reference_data.gsheet_priezdi_marcar` лежит ссылка на карточку CRM
# (`.../crm.marcar.ru/.../<id>`); хвост после последнего `/` == `leads_all.source_record_id`.
# Патч НЕ перезаписывает статус «вниз» по воронке: применяется, только если
# приоритет gsheet-статуса СТРОГО выше приоритета текущего CRM-статуса
# (0 = самый высокий). Статус вне списка = приоритет 9999, т.е. любой из четырёх
# gsheet-статусов его перебивает — ровно как в v5.
#
# ⚠️ Парная половина правки — КАТЕГОРИИ этих статусов. В CH-справочнике нет
# general-ветки (v5 `crm_name='default'`), а прав на запись в `raw_data.*` нет,
# поэтому категории заданы кодом: `step3_build_sources/step3.py::CODE_STATUS_CATEGORY`
# (маркер CODE_STATUS_CATEGORY_2026-08-06). Добавили сюда пятый статус — обязаны
# добавить его туда же, иначе `check_code_status_categories()` уронит шаг 3.
# ══════════════════════════════════════════════════════════════════════════════
MARCAR_SOURCE_TYPE = "marcar_crm_excel"

_PERFORM_SOURCE_NAMES = (
    "LeadVDL Perform Южный Обход",
    "LeadVDL Perform Автопарк Южный",
    "LeadVDL Perform Кубань Драйв",
    "LeadVDL Perform АвтоМаркет",
    "LeadVDL Perform Нижний Центр Авто",
    "LeadVDL Perform Нави Кар",
    "LeadV Perform Автопарк Южный",
    "LeadV Perform Южный Обход",
    "LeadV Perform Нижний Центр Авто",
    "LeadV Perform АвтоМаркет",
    "LeadV Perform Нави Кар",
    "LeadV Perform Кубань Драйв",
)

_PERFORM_CRMF_EXTRA_SOURCE_NAMES = (
    "LeadVDL 2 Эйс Авто",
    "LeadV 2 Эйс Авто",
    "LeadV ПБ Эйс Авто",
    "LeadV ПБО Эйс Авто",
    "LeadVDLS Эйс Авто",
    "LeadVDL Лидер",
    "LeadV Лидер",
    "LeadVDL 2 Лидер",
    "LeadVDL 2 Лидер Авто НСК",
    "LeadV ACB Лидер Авто НСК",
    "LeadV Лидер Авто НСК",
    "LeadVDL 3 Лидер Авто НСК",
    "LeadVDLS Лидер Авто НСК",
    "LeadПБ Лидер Авто НСК",
)

_PERFORM_MAUTO_SOURCE_NAMES = (
    "LeadV Перформ Лидер",
    "LeadV Перформ КТ Лидер",
)

# Иерархия статусов Маркара (0 = высший). Копия v5 `_MARCAR_STATUS_PRIORITY` (v5 step0.py:1145).
MARCAR_STATUS_PRIORITY: dict[str, int] = {
    "Продажа": 0,
    "Дошел в КО": 1,
    "Одобрение": 2,
    "Приехал": 3,
}


def _marcar_priority_expr(status_expr: str) -> str:
    branches = "".join(f"{status_expr} = '{status}', {prio}, " for status, prio in MARCAR_STATUS_PRIORITY.items())
    return f"multiIf({branches}9999)"


def _marcar_gsheet_subquery() -> str:
    """id карточки Маркара -> лучший (самый глубокий) статус из гугл-таблицы приездов."""
    statuses_in = ", ".join(f"'{status}'" for status in MARCAR_STATUS_PRIORITY)
    return f"""
SELECT
    lead_record_id,
    argMin(status, prio) AS status
FROM
(
    SELECT
        replaceRegexpOne(ifNull(link, ''), '^.+/', '') AS lead_record_id,
        ifNull(status, '') AS status,
        {_marcar_priority_expr("ifNull(status, '')")} AS prio
    FROM reference_data.gsheet_priezdi_marcar
    WHERE ifNull(link, '') LIKE '%crm.marcar.ru%'
      AND match(ifNull(link, ''), '^https?://.+/[0-9]+$')
      AND ifNull(status, '') IN ({statuses_in})
)
GROUP BY lead_record_id
"""


def _marcar_join_sql(lead_alias: str, join_alias: str = "mp") -> str:
    return f"""
LEFT JOIN ({_marcar_gsheet_subquery()}) AS {join_alias}
  ON {join_alias}.lead_record_id = ifNull({lead_alias}.source_record_id, '')
"""


def _marcar_patched_status_expr(lead_alias: str, join_alias: str = "mp") -> str:
    """status лида с применённым патчем Маркара (только «вверх» по воронке)."""
    current = f"ifNull({lead_alias}.status, '')"
    patched = f"ifNull({join_alias}.status, '')"
    return f"""
    if(
        {lead_alias}.source_type = '{MARCAR_SOURCE_TYPE}'
        AND {patched} != ''
        AND {_marcar_priority_expr(patched)} < {_marcar_priority_expr(current)},
        CAST({join_alias}.status, 'Nullable(String)'),
        {lead_alias}.status
    )
"""


def _quoted_csv(values: tuple[str, ...]) -> str:
    return ", ".join("'{}'".format(value.replace("'", "''")) for value in values)


def _perform_phone_norm_expr(expr: str) -> str:
    return f"right(replaceRegexpAll(ifNull({expr}, ''), '[^0-9]', ''), 10)"


def _perform_cohort_condition(alias: str, *, include_extra: bool) -> str:
    crmf_names = _PERFORM_SOURCE_NAMES + (_PERFORM_CRMF_EXTRA_SOURCE_NAMES if include_extra else ())
    return (
        f"(({alias}.source_type = 'crmf_excel' AND {alias}.source_name IN ({_quoted_csv(crmf_names)})) "
        f"OR ({alias}.source_type = 'mauto_excel' AND {alias}.source_name IN ({_quoted_csv(_PERFORM_MAUTO_SOURCE_NAMES)}))"
        + (
            f" OR ({alias}.source_type IN ('plex_excel', 'genzes_excel') "
            f"AND positionCaseInsensitive(ifNull({alias}.utm_source, ''), 'perform') > 0)"
            if include_extra
            else ""
        )
        + ")"
    )


def _drop_and_create(client, table: str, create_sql: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {table} SYNC")
    client.command(create_sql, settings=SAFE_QUERY_SETTINGS)


def _command_with_retry(client, sql: str, *, label: str, settings=None, attempts: int = 3):
    for attempt in range(1, attempts + 1):
        try:
            client.command(sql, settings=settings)
            return client
        except Exception as exc:
            if attempt == attempts:
                raise
            logger.warning(
                "    %s failed (%d/%d): %s; reconnecting",
                label,
                attempt,
                attempts,
                exc,
            )
            time.sleep(attempt * 2)
            client = get_client()
    return client


def _total_cost_expr() -> str:
    """Source-level overrides for raw_data rows where corrected total_cost is missing."""
    branches: list[str] = []
    for rule in DIRECT_TOTAL_COST_FACTOR_OVERRIDES:
        domain_condition = ""
        if rule.get("domain"):
            domain_condition = f" AND lower(ifNull(domain, '')) = '{rule['domain']}'"
        source_value = (
            f"toFloat64(ifNull(total_cost, 0)) * {rule['factor']}"
            if rule.get("factor")
            else f"toDecimal64(ifNull(cost, 0), 9) * {rule['multiplier']} / {rule['divisor']}"
        )
        branches.extend(
            [
                (
                    f"client_login = '{rule['account_login']}' "
                    f"AND toDate(day) >= toDate('{rule['date_from']}') "
                    f"AND toDate(day) < toDate('{rule['date_to']}') "
                    f"{domain_condition} "
                    + (
                        "AND ifNull(total_cost, 0) = ifNull(cost, 0)"
                        if rule.get("require_equal_cost", True)
                        else "AND ifNull(total_cost, 0) != 0"
                    )
                ),
                f"toDecimal64({source_value}, 9)",
            ]
        )
    branches.append("toDecimal64(ifNull(total_cost, 0), 9)")
    return f"multiIf({', '.join(branches)})"


# RAW_YANDEX_WEIGHT_2026-08-14 (OPTIMIZATION_PLAN.md, фаза 1+2.1): схема задаётся явно, а не
# выводится из SELECT. Три правки, вместе −63.7% веса таблицы на пробе партиции 202607:
#   * колонка `id` (cityHash64(row_key)) убрана — 198 МиБ = 32% таблицы, ни один потребитель её
#     не читает, в ORDER BY её нет, случайный хэш не сжимается;
#   * LowCardinality на словарных строках (Device 4 значения, AdNetworkType 2, manager_login 5,
#     account_login 1060, CampaignName 19.3k, AdGroupName 44.9k);
#   * кодеки на числовых: T64+ZSTD(3) для Decimal(18,9) и Int64 (замерено: лучше чистого ZSTD
#     и Delta именно на этой колонке), ZSTD(3) на длинном строковом ключе key3.
# Порядок колонок обязан совпадать с порядком в _raw_yandex_select: INSERT ... SELECT позиционный.
_RAW_YANDEX_COLUMNS = """
    `Date` Date,
    `CampaignId` Int64 CODEC(T64, ZSTD(3)),
    `CampaignName` LowCardinality(Nullable(String)),
    `AdGroupId` Int64 CODEC(T64, ZSTD(3)),
    `AdGroupName` LowCardinality(Nullable(String)),
    `AdNetworkType` LowCardinality(Nullable(String)),
    `Device` LowCardinality(Nullable(String)),
    `RlAdjustmentId` Int64 CODEC(T64, ZSTD(3)),
    `Impressions` Int64 CODEC(T64, ZSTD(3)),
    `Clicks` Int64 CODEC(T64, ZSTD(3)),
    `total_cost` Decimal(18, 9) CODEC(T64, ZSTD(3)),
    `account_login` LowCardinality(String),
    `manager_login` LowCardinality(String),
    `adgroup_code` LowCardinality(Nullable(String)),
    `campaign_code` LowCardinality(Nullable(String)),
    `tp` LowCardinality(String),
    `cpc_cpa` LowCardinality(String),
    `site_quiz` LowCardinality(String),
    `week_start` Date,
    `key3` String CODEC(ZSTD(3))
"""


def _raw_yandex_ddl() -> str:
    """Пустая raw_yandex с явными типами и кодеками. Данные заливаются батчами отдельно."""
    return f"""
CREATE TABLE {RAW_TARGET_TABLES["raw_yandex"]}
({_RAW_YANDEX_COLUMNS})
ENGINE = MergeTree
PARTITION BY toYYYYMM("Date")
ORDER BY ("Date", "CampaignId", key3)
"""


def _raw_yandex_select(raw_date_filter: str = "", source: str = "raw_data.yandex_direct_report_rows") -> str:
    total_cost_expr = _total_cost_expr()
    return f"""
WITH parsed_src AS
(
    SELECT
        toDate(day) AS "Date",
        campaign_id AS "CampaignId",
        campaign_name AS "CampaignName",
        ifNull(ad_group_id, 0) AS "AdGroupId",
        ad_group_name AS "AdGroupName",
        ad_network_type AS "AdNetworkType",
        device AS "Device",
        toInt64OrZero(ifNull(rl_adjustment_id, '')) AS "RlAdjustmentId",
        ifNull(impressions, 0) AS "Impressions",
        ifNull(clicks, 0) AS "Clicks",
        {total_cost_expr} AS total_cost,
        client_login AS account_login,
        manager_login AS manager_login,
        nullIf(extract(
            splitByString(' — ', ifNull(ad_group_name, ''))[1],
            '(ct\\\\d+_(?:aoff|aon)_n\\\\d+_r\\\\d+_ct\\\\d+_ag\\\\d+_g\\\\d+)'
        ), '') AS adgroup_code,
        nullIf(extract(
            replaceRegexpAll(replaceAll(ifNull(campaign_name, ''), 'с', 'c'), '__+', '_'),
            '(?i)(tp\\\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'
        ), '') AS campaign_match,
        match(ifNull(campaign_name, ''), '(?i)tp[67]_') AS tp67_check,
        toStartOfWeek(toDate(day), 1) AS week_start
    FROM {source}
    WHERE toDate(day) >= toDate('{DATE_FROM}')
      AND campaign_id != 0
      {raw_date_filter}
)
SELECT
    "Date",
    "CampaignId",
    "CampaignName",
    "AdGroupId",
    "AdGroupName",
    "AdNetworkType",
    "Device",
    "RlAdjustmentId",
    "Impressions",
    "Clicks",
    total_cost,
    account_login,
    manager_login,
    adgroup_code,
    campaign_match AS campaign_code,
    lower(splitByChar('_', ifNull(campaign_match, ''))[1]) AS tp,
    lower(splitByChar('_', ifNull(campaign_match, ''))[2]) AS cpc_cpa,
    replaceAll(lower(splitByChar('_', ifNull(campaign_match, ''))[3]), 'kviz', 'quiz') AS site_quiz,
    week_start,
    lower(concat(
        toString("Date"), '|',
        toString("CampaignId"),
        if(tp67_check, '|0', concat('|', toString("AdGroupId"))), '|',
        multiIf(
            lower(ifNull("Device", '')) = 'mobile', 'mobile',
            lower(ifNull("Device", '')) = 'desktop', 'desktop',
            lower(ifNull("Device", '')) = 'tablet', 'tablet',
            lower(ifNull("Device", '')) = 'smart_tv', 'smart_tv',
            '0'
        ), '|',
        toString("RlAdjustmentId")
    )) AS key3
FROM parsed_src
"""


def _direct_source_table(client) -> str:
    primary = "raw_data.yandex_direct_report_rows"
    if table_exists(client, "raw_data", "yandex_direct_report_rows"):
        rows = count_rows(client, primary)
        if rows > 0:
            logger.info("  raw Direct source: %s (%d rows)", primary, rows)
            return primary
    fallback = "ad_analytics.local_yandex_pg"
    if table_exists(client, "ad_analytics", "local_yandex_pg"):
        rows = count_rows(client, fallback)
        if rows > 0:
            logger.warning("  raw Direct source fallback: %s (%d rows)", fallback, rows)
            return fallback
    return primary


def _raw_leads_select_sql(source: str = "raw_data.leads_all") -> str:
    excluded_domain_clause = _excluded_domain_filter_sql("d.domain")
    return f"""
SELECT
    -- явный алиас обязателен: с третьим JOIN (mp) анализатор CH называет колонку `l.id`,
    -- и `raw_leads.id` переименовывается — step3 (`ORDER BY … id`) падает
    l.id AS id,
    l.created_date,
    l.arrival_date,
    l.domain_id,
    d.domain AS domain,
    l.deal_type,
    -- MARCAR_GSHEET_STATUS_2026-08-05
    {_marcar_patched_status_expr("l")} AS status,
    l.source_type,
    l.campaign_id,
    l.group_id,
    l.correction_id,
    l.utm_source,
    l.utm_medium,
    l.utm_campaign,
    l.utm_content,
    l.utm_term,
    l.phone,
    l.yclid,
    l.is_copy_for_removal,
    l.reason,
    l.salon,
    nullIf(extract(ifNull(l.utm_content, ''), 'fid:([^|]+)'), '') AS fid,
    lower(concat(
        ifNull(toString(l.created_date), ''), '|',
        toString(ifNull(l.campaign_id, 0)),
        if(match(ifNull(l.utm_campaign, ''), '(?i)tp[67]'), '|0', concat('|', toString(ifNull(l.group_id, 0)))), '|',
        multiIf(
            position(ifNull(l.utm_content, ''), 'dev:mobile') > 0, 'mobile',
            position(ifNull(l.utm_content, ''), 'dev:desktop') > 0, 'desktop',
            position(ifNull(l.utm_content, ''), 'dev:tablet') > 0, 'tablet',
            position(ifNull(l.utm_content, ''), 'dev:smart_tv') > 0, 'smart_tv',
            '0'
        ), '|',
        toString(ifNull(l.correction_id, 0))
    )) AS key3,
    if(l.arrival_date IS NULL, NULL, lower(concat(
        ifNull(toString(l.arrival_date), ''), '|',
        toString(ifNull(l.campaign_id, 0)),
        if(match(ifNull(l.utm_campaign, ''), '(?i)tp[67]'), '|0', concat('|', toString(ifNull(l.group_id, 0)))), '|',
        multiIf(
            position(ifNull(l.utm_content, ''), 'dev:mobile') > 0, 'mobile',
            position(ifNull(l.utm_content, ''), 'dev:desktop') > 0, 'desktop',
            position(ifNull(l.utm_content, ''), 'dev:tablet') > 0, 'tablet',
            position(ifNull(l.utm_content, ''), 'dev:smart_tv') > 0, 'smart_tv',
            '0'
        ), '|',
        toString(ifNull(l.correction_id, 0))
    ))) AS key3_arrival_date
FROM {source} AS l
LEFT JOIN reference_data.domains AS d ON d.id = l.domain_id
{_marcar_join_sql("l")}
WHERE l.deal_type != 'Звонок'
  AND ifNull(l.is_copy_for_removal, 0) = 0
  -- EXCLUDED_DOMAIN_NAMES_2026-08-06: матч по ИМЕНИ домена (d.domain, уже заджойнен выше),
  -- не по числовому id (id непереносим между PG v5 и CH v6 — см. config/ch_settings.py).
  -- ifNull(d.domain, '') естественно пропускает лиды с NULL domain_id (легитимные лиды без
  -- разрешённого FK — d.domain тоже NULL после LEFT JOIN), без отдельного `OR domain_id IS NULL`.
  -- EXCLUDED_DOMAIN_NAMES_GUARD_2026-08-06: пустой EXCLUDED_DOMAIN_NAMES → excluded_domain_clause
  -- пустая строка (условие не подставляется), не `NOT IN ()` (синтаксическая ошибка в ClickHouse).
{excluded_domain_clause}"""


def _leads_source_table(client) -> str:
    primary = "raw_data.leads_all"
    if table_exists(client, "raw_data", "leads_all"):
        rows = count_rows(client, primary)
        if rows > 0:
            logger.info("  raw CRM source: %s (%d rows)", primary, rows)
            return primary
    fallback = "ad_analytics.local_leads_all_pg"
    if table_exists(client, "ad_analytics", "local_leads_all_pg"):
        rows = count_rows(client, fallback)
        if rows > 0:
            logger.warning("  raw CRM source fallback: %s (%d rows)", fallback, rows)
            return fallback
    return primary


def _raw_leads_sql(source_filter: str = "", source: str = "raw_data.leads_all") -> str:
    table = RAW_TARGET_TABLES["raw_leads"]
    return f"""
CREATE TABLE {table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(ifNull(created_date, toDate('{DATE_FROM}')))
ORDER BY (ifNull(created_date, toDate('{DATE_FROM}')), ifNull(domain_id, 0), key3)
AS
SELECT * FROM ({_raw_leads_select_sql(source)})
WHERE 1 = 1
  {source_filter}
"""


def _raw_calls_sql(source_filter: str = "", source: str = "raw_data.leads_all") -> str:
    table = RAW_TARGET_TABLES["raw_calls"]
    return f"""
CREATE TABLE {table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(ifNull(created_date, toDate('{DATE_FROM}')))
ORDER BY (ifNull(created_date, toDate('{DATE_FROM}')), ifNull(domain_id, 0), id)
AS
SELECT
    l.id AS id,  -- см. комментарий в _raw_leads_select_sql: алиас держит имя колонки
    l.created_date,
    l.domain_id,
    d.domain AS domain,
    l.deal_type,
    -- MARCAR_GSHEET_STATUS_2026-08-05: в v5 патч был UPDATE по local_leads_all,
    -- т.е. накрывал и звонки (deal_type='Звонок') — здесь то же выражение.
    {_marcar_patched_status_expr("l")} AS status,
    l.reason,
    l.source_type,
    l.phone,
    l.utm_source,
    l.utm_medium,
    l.utm_campaign
FROM {source} AS l
LEFT JOIN reference_data.domains AS d ON d.id = l.domain_id
{_marcar_join_sql("l")}
WHERE l.deal_type = 'Звонок'
  AND ifNull(l.is_copy_for_removal, 0) = 0
  AND l.domain_id IS NOT NULL
  {source_filter}
"""


def _raw_domains_select_sql() -> str:
    return """
SELECT
    id,
    domain AS name,
    domain,
    niche,
    counter_id,
    counter_name,
    metrika_synced_at,
    created_at
FROM reference_data.domains
"""


def _replace_raw_domains_view(client) -> int:
    replace_view(client, RAW_TARGET_TABLES["raw_domains"], _raw_domains_select_sql())
    return count_rows(client, RAW_TARGET_TABLES["raw_domains"])


def _raw_perform_leads_sql() -> str:
    table = RAW_TARGET_TABLES["raw_perform_leads"]
    excluded_domain_clause = _excluded_domain_filter_sql("ifNull(nullIf(pl.domain, ''), d.domain)")
    phone_norm_pl = _perform_phone_norm_expr("pl.phone")
    phone_norm_la = _perform_phone_norm_expr("la.phone")
    phone_norm_l = _perform_phone_norm_expr("l.phone")
    status_expr = "coalesce(nullIf(trim(ifNull(m.status, '')), ''), 'без статуса')"
    return f"""
CREATE TABLE {table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(ifNull(created_date, toDate('{DATE_FROM}')))
ORDER BY (ifNull(created_date, toDate('{DATE_FROM}')), ifNull(domain_id, 0), key3)
AS
WITH
crm_status_ranked AS
(
    SELECT
        status,
        argMin(category, multiIf(crm IN ('crmf', 'mauto'), 1, crm IN ('', 'default'), 2, 3)) AS category
    FROM reference_data.crm_status_mapping
    WHERE ifNull(status, '') != ''
      AND ifNull(salon, '') = ''
    GROUP BY status
),
sale_statuses AS
(
    SELECT DISTINCT status
    FROM reference_data.crm_status_mapping
    WHERE category = 'sale'
      AND ifNull(status, '') != ''
      AND ifNull(salon, '') = ''
),
matched AS
(
    SELECT
        {phone_norm_la} AS phone_norm,
        argMin(
            ifNull(la.status, ''),
            tuple(
                multiIf(
                    cs.category = 'sale', 1,
                    cs.category = 'visit', 2,
                    cs.category = 'qualified', 3,
                    cs.category = 'correct', 4,
                    cs.category = 'incorrect', 5,
                    6
                ),
                -toUnixTimestamp(toDateTime(ifNull(la.created_date, toDate('1970-01-01')))),
                la.id
            )
        ) AS status
    FROM raw_data.leads_all AS la
    LEFT JOIN crm_status_ranked AS cs ON cs.status = ifNull(la.status, '')
    WHERE {phone_norm_la} != ''
      AND ifNull(la.is_copy_for_removal, 0) = 0
      AND {_perform_cohort_condition("la", include_extra=True)}
    GROUP BY phone_norm
),
perform_phones AS
(
    SELECT DISTINCT {phone_norm_pl} AS phone_norm
    FROM raw_data.perform_leads AS pl
    WHERE {phone_norm_pl} != ''
),
branch_b_conflict_phones AS
(
    SELECT {phone_norm_l} AS phone_norm
    FROM raw_data.leads_all AS l
    WHERE {phone_norm_l} != ''
      AND ifNull(l.is_copy_for_removal, 0) = 0
      AND {_perform_cohort_condition("l", include_extra=False)}
    GROUP BY phone_norm
    HAVING countIf(ifNull(l.status, '') IN (SELECT status FROM sale_statuses)) > 0
       AND countIf(ifNull(l.status, '') NOT IN (SELECT status FROM sale_statuses)) > 0
       AND uniqExact(ifNull(l.source_name, '')) > 1
),
perform_vk_phones AS
(
    SELECT DISTINCT {phone_norm_l} AS phone_norm
    FROM raw_data.leads_all AS l
    WHERE {phone_norm_l} != ''
      AND ifNull(l.is_copy_for_removal, 0) = 0
      AND l.source_type = 'crmf_excel'
      AND ifNull(l.utm_source, '') = 'vkads'
      AND ifNull(l.utm_campaign, '') = 'victory'
)
SELECT
    id, created_date, arrival_date, domain_id, domain, deal_type, status,
    source_type, campaign_id, group_id, correction_id, utm_source, utm_medium,
    utm_campaign, utm_content, utm_term, phone, yclid, is_copy_for_removal,
    reason, salon, fid, key3, key3_arrival_date
FROM
(
    SELECT a.*,
        row_number() OVER (
            PARTITION BY a._phone_norm
            ORDER BY
                (1 - a._is_sale),
                if(a.arrival_date IS NOT NULL, 0, 1),
                a.created_date,
                ifNull(a.domain_id, 0),
                a.id
        ) AS _rn,
        min(ifNull(a.domain_id, 0)) OVER (PARTITION BY a._phone_norm) AS _dmin,
        max(ifNull(a.domain_id, 0)) OVER (PARTITION BY a._phone_norm) AS _dmax,
        max(a._is_sale) OVER (PARTITION BY a._phone_norm) AS _hassale
    FROM
    (
        SELECT
            pl.id AS id,
            pl.created_date,
            pl.arrival_date,
            CAST(coalesce(d.id, pl.domain_id), 'Nullable(Int64)') AS domain_id,
            ifNull(nullIf(pl.domain, ''), d.domain) AS domain,
            pl.deal_type,
            {status_expr} AS status,
            pl.source_type,
            pl.campaign_id,
            pl.group_id,
            pl.correction_id,
            pl.utm_source,
            pl.utm_medium,
            pl.utm_campaign,
            pl.utm_content,
            pl.utm_term,
            pl.phone,
            pl.yclid,
            pl.is_copy_for_removal,
            pl.reason,
            pl.salon,
            nullIf(extract(ifNull(pl.utm_content, ''), 'fid:([^|]+)'), '') AS fid,
            lower(concat(
                ifNull(toString(pl.created_date), ''), '|',
                toString(ifNull(pl.campaign_id, 0)),
                if(match(ifNull(pl.utm_campaign, ''), '(?i)tp[67]'), '|0', concat('|', toString(ifNull(pl.group_id, 0)))), '|',
                multiIf(
                    position(ifNull(pl.utm_content, ''), 'dev:mobile') > 0, 'mobile',
                    position(ifNull(pl.utm_content, ''), 'dev:desktop') > 0, 'desktop',
                    position(ifNull(pl.utm_content, ''), 'dev:tablet') > 0, 'tablet',
                    position(ifNull(pl.utm_content, ''), 'dev:smart_tv') > 0, 'smart_tv',
                    '0'
                ), '|',
                toString(ifNull(pl.correction_id, 0))
            )) AS key3,
            if(pl.arrival_date IS NULL, NULL, lower(concat(
                ifNull(toString(pl.arrival_date), ''), '|',
                toString(ifNull(pl.campaign_id, 0)),
                if(match(ifNull(pl.utm_campaign, ''), '(?i)tp[67]'), '|0', concat('|', toString(ifNull(pl.group_id, 0)))), '|',
                multiIf(
                    position(ifNull(pl.utm_content, ''), 'dev:mobile') > 0, 'mobile',
                    position(ifNull(pl.utm_content, ''), 'dev:desktop') > 0, 'desktop',
                    position(ifNull(pl.utm_content, ''), 'dev:tablet') > 0, 'tablet',
                    position(ifNull(pl.utm_content, ''), 'dev:smart_tv') > 0, 'smart_tv',
                    '0'
                ), '|',
                toString(ifNull(pl.correction_id, 0))
            ))) AS key3_arrival_date,
            {phone_norm_pl} AS _phone_norm,
            if({status_expr} IN (SELECT status FROM sale_statuses), 1, 0) AS _is_sale
        FROM raw_data.perform_leads AS pl
        LEFT JOIN reference_data.domains AS d ON lowerUTF8(trim(d.domain)) = lowerUTF8(trim(pl.domain))
        LEFT JOIN matched AS m ON m.phone_norm = {phone_norm_pl}
        WHERE (pl.deal_type IS NULL OR pl.deal_type != 'Звонок')
          AND {phone_norm_pl} NOT IN (SELECT phone_norm FROM perform_vk_phones)
{excluded_domain_clause}
    ) AS a
) AS dd
WHERE NOT (_phone_norm != '' AND _dmin != _dmax AND _hassale = 1 AND _rn > 1)
UNION ALL
SELECT
    l.id,
    l.created_date,
    CAST(NULL, 'Nullable(Date)') AS arrival_date,
    CAST(d_perf.id, 'Nullable(Int64)') AS domain_id,
    d_perf.domain AS domain,
    CAST(NULL, 'Nullable(String)') AS deal_type,
    l.status,
    'perform_api' AS source_type,
    CAST(NULL, 'Nullable(Int64)') AS campaign_id,
    CAST(NULL, 'Nullable(Int64)') AS group_id,
    CAST(NULL, 'Nullable(Int64)') AS correction_id,
    CAST(NULL, 'Nullable(String)') AS utm_source,
    CAST(NULL, 'Nullable(String)') AS utm_medium,
    CAST(NULL, 'Nullable(String)') AS utm_campaign,
    CAST(NULL, 'Nullable(String)') AS utm_content,
    CAST(NULL, 'Nullable(String)') AS utm_term,
    l.phone,
    CAST(NULL, 'Nullable(String)') AS yclid,
    l.is_copy_for_removal,
    l.reason,
    l.salon,
    CAST(NULL, 'Nullable(String)') AS fid,
    '' AS key3,
    CAST(NULL, 'Nullable(String)') AS key3_arrival_date
FROM raw_data.leads_all AS l
CROSS JOIN
(
    SELECT id, domain
    FROM reference_data.domains
    WHERE domain = 'cars-rus.ru'
    LIMIT 1
) AS d_perf
WHERE (l.deal_type IS NULL OR l.deal_type != 'Звонок')
  AND ifNull(l.is_copy_for_removal, 0) = 0
  AND ifNull(l.phone, '') != ''
  AND {_perform_cohort_condition("l", include_extra=False)}
  AND {phone_norm_l} NOT IN (SELECT phone_norm FROM perform_phones)
  AND {phone_norm_l} NOT IN (SELECT phone_norm FROM perform_vk_phones)
  AND NOT (
      ifNull(l.status, '') IN (SELECT status FROM sale_statuses)
      AND {phone_norm_l} IN (SELECT phone_norm FROM branch_b_conflict_phones)
  )
"""


def _select_from_create(create_sql: str) -> str:
    marker = "\nAS\n"
    if marker not in create_sql:
        raise ValueError("CREATE TABLE SQL does not contain expected AS marker")
    return create_sql.split(marker, 1)[1].strip()


def _rebuild_batched(client, logical_name: str, empty_sql: str, batch_selects: list[str]):
    table = RAW_TARGET_TABLES[logical_name]
    shadow = f"{table}_new"
    client = _command_with_retry(client, f"DROP TABLE IF EXISTS {shadow} SYNC", label=f"{logical_name} shadow drop")
    client = _command_with_retry(
        client,
        empty_sql.replace(table, shadow, 1),
        label=f"{logical_name} shadow create",
        settings=SAFE_QUERY_SETTINGS,
    )
    # RAW_WEIGHT_2026-08-14: у raw_leads/raw_calls схема выводится из SELECT — кодеки навешиваем
    # на пустую shadow. Для raw_yandex они уже прописаны в DDL, повторный ALTER идемпотентен.
    apply_storage_codecs(client, shadow)
    for idx, select_sql in enumerate(batch_selects, start=1):
        client = _command_with_retry(
            client,
            f"INSERT INTO {shadow}\n{select_sql}",
            label=f"{logical_name} daily batch {idx}/{len(batch_selects)}",
            settings=SAFE_QUERY_SETTINGS,
        )
        logger.info("    %s daily batch %d/%d inserted", logical_name, idx, len(batch_selects))
    swap_shadow(client, table, shadow)
    return client, count_rows(client, table)


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 1 v6_ch: пересборка RAW таблиц в ClickHouse")
    client = get_client()
    t0 = time.perf_counter()

    details_parts: list[str] = []
    total = 0
    direct_source = _direct_source_table(client)
    leads_source = _leads_source_table(client)

    t_table = time.perf_counter()
    yandex_ranges = day_ranges(DATE_FROM)
    yandex_batches = [
        _raw_yandex_select(
            f"AND toDate(day) >= toDate('{lo}') AND toDate(day) < toDate('{hi}')",
            source=direct_source,
        )
        for lo, hi in yandex_ranges
    ]
    logger.info("  rebuild %s (%d daily batches)", RAW_TARGET_TABLES["raw_yandex"], len(yandex_batches))
    client, rows = _rebuild_batched(client, "raw_yandex", _raw_yandex_ddl(), yandex_batches)
    total += rows
    details_parts.append(f"raw_yandex={rows:,}")
    logger.info("  raw_yandex: %d строк за %.1f сек", rows, time.perf_counter() - t_table)

    lead_ranges = day_ranges(DATE_FROM)
    t_table = time.perf_counter()
    lead_batches = [
        _select_from_create(
            _raw_leads_sql(
                f"AND created_date >= toDate('{lo}') AND created_date < toDate('{hi}')",
                source=leads_source,
            )
        )
        for lo, hi in lead_ranges
    ]
    logger.info("  rebuild %s (%d daily batches)", RAW_TARGET_TABLES["raw_leads"], len(lead_batches))
    client, rows = _rebuild_batched(client, "raw_leads", _raw_leads_sql("AND 0", source=leads_source), lead_batches)
    total += rows
    details_parts.append(f"raw_leads={rows:,}")
    logger.info("  raw_leads: %d строк за %.1f сек", rows, time.perf_counter() - t_table)

    t_table = time.perf_counter()
    call_batches = [
        _select_from_create(
            _raw_calls_sql(
                f"AND created_date >= toDate('{lo}') AND created_date < toDate('{hi}')",
                source=leads_source,
            )
        )
        for lo, hi in lead_ranges
    ]
    logger.info("  rebuild %s (%d daily batches)", RAW_TARGET_TABLES["raw_calls"], len(call_batches))
    client, rows = _rebuild_batched(client, "raw_calls", _raw_calls_sql("AND 0", source=leads_source), call_batches)
    total += rows
    details_parts.append(f"raw_calls={rows:,}")
    logger.info("  raw_calls: %d строк за %.1f сек", rows, time.perf_counter() - t_table)

    t_table = time.perf_counter()
    logger.info("  replace %s as VIEW", RAW_TARGET_TABLES["raw_domains"])
    rows = _replace_raw_domains_view(client)
    total += rows
    details_parts.append(f"raw_domains={rows:,}")
    logger.info("  raw_domains view: %d строк за %.1f сек", rows, time.perf_counter() - t_table)

    for logical_name, sql in [
        ("raw_perform_leads", _raw_perform_leads_sql()),
    ]:
        table = RAW_TARGET_TABLES[logical_name]
        t_table = time.perf_counter()
        logger.info("  rebuild %s", table)
        _drop_and_create(client, table, sql)
        rows = count_rows(client, table)
        total += rows
        details_parts.append(f"{logical_name}={rows:,}")
        logger.info("  %s: %d строк за %.1f сек", logical_name, rows, time.perf_counter() - t_table)

    cost_sum = client.query(
        f"SELECT sum(total_cost) FROM {RAW_TARGET_TABLES['raw_yandex']}",
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows[0][0]
    if not cost_sum:
        raise RuntimeError(
            f"RAW_YANDEX_COST_GUARD: {RAW_TARGET_TABLES['raw_yandex']}.total_cost=0 после загрузки"
        )
    logger.info("RAW_YANDEX_COST_GUARD: SUM(total_cost)=%s — OK", cost_sum)

    details = ", ".join(details_parts)
    logger.info("Шаг 1 v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": total, "details": details}


def get_explain_sql(conn=None) -> str:  # noqa: ARG001
    return f"SELECT count() FROM {RAW_TARGET_TABLES['raw_yandex']}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
