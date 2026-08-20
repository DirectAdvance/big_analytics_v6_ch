"""Step 10 for v6_ch: add crop/Telega/VK cost overlays and Telega.in lead facts."""

from __future__ import annotations

import json
import logging
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM, VK_AUTO_ACCOUNTS_SQL
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, day_ranges, replace_view, swap_shadow, table_exists
from step3_build_sources.step3 import CROP_SOURCE_TYPES, CRM_NAME_BY_SOURCE_TYPE, SOURCE_STORE

logger = logging.getLogger("pipeline.step10")

COST_OVERLAY_TABLE = "big_analytics_cost_overlays"
TELEGA_PRICE_OVERRIDES = "telega_in_order_price_overrides"
TELEGA_FIELD_OVERRIDES = "telega_in_order_field_overrides"
JOIN_QUERY_SETTINGS = {**SAFE_QUERY_SETTINGS, "join_use_nulls": 1}
CROP_TYPES_SQL = ", ".join(f"'{source_type}'" for source_type in CROP_SOURCE_TYPES)


def _crm_by_domain_cte() -> str:
    branches = "".join(
        f"has(groupArray(source_type), '{source_type}'), '{crm_name}', "
        for source_type, crm_name in sorted(CRM_NAME_BY_SOURCE_TYPE.items())
    )
    return f"""
        crm_by_domain AS
        (
            SELECT
                domain_key,
                multiIf({branches}ifNull(nullIf(anyLast(source_type), ''), 'Не указана')) AS crm_name
            FROM
            (
                SELECT lowerUTF8(trim(ifNull(domain, ''))) AS domain_key, source_type FROM ad_analytics.raw_leads
                UNION ALL
                SELECT lowerUTF8(trim(ifNull(domain, ''))) AS domain_key, source_type FROM ad_analytics.raw_calls
            )
            WHERE domain_key != ''
            GROUP BY domain_key
        )
    """


_GS_DATE = "assumeNotNull(toDate(parseDateTimeBestEffortOrNull(ifNull(g.`Дата`, ''))))"
_GS_COST = (
    "ifNull(toDecimal64OrNull(replaceAll(replaceRegexpAll(ifNull(g.total_cost, ''), '[^0-9,.-]', ''), ',', '.'), 6), "
    "toDecimal64(0, 6))"
)
_GS_SOURCE = """
    coalesce(
        multiIf(
            trim(ifNull(g.`Источник`, '')) = 'Telegram', 'Посевы_Telegram',
            trim(ifNull(g.`Источник`, '')) = 'VK', 'Посевы_VK',
            trim(ifNull(g.`Источник`, '')) = 'Max', 'Посевы_Max',
            nullIf(trim(ifNull(g.`Источник`, '')), '')
        ),
        multiIf(
            match(lowerUTF8(ifNull(g.`utm утвержденная`, '')), '(^|_)vk($|_|[0-9])'), 'Посевы_VK',
            match(lowerUTF8(ifNull(g.`utm утвержденная`, '')), '(^|_)max($|_|[0-9])'), 'Посевы_Max',
            'Посевы_Telegram'
        )
    )
"""
_API_SOURCE = """
    multiIf(
        lowerUTF8(ifNull(t.`источник`, '')) IN ('telegram', 'instagram'), 'Посевы_Telegram',
        ifNull(t.`источник`, '') = 'VK', 'Посевы_VK',
        ifNull(t.`источник`, '') = 'Max', 'Посевы_Max',
        ifNull(t.`источник`, '') = '', 'Посевы_Telegram',
        concat('Посевы_', ifNull(t.`источник`, ''))
    )
"""


def _in_status(status_expr: str, values: tuple[str, ...]) -> str:
    return f"ifNull({status_expr}, '') IN ({', '.join(repr(value) for value in values)})"


def _telega_api_metric_expr(status_expr: str = "status") -> str:
    """BA5 Telega.in API formula: kval = korr - no-answer - filter - no-call."""
    korr = (
        'Новый', 'В салоне', 'Купил', 'На рассмотрении', 'В салоне не отмечен', 'Отказ',
        'Не отвечает', 'Приедет', 'В работе', 'Фильтр', 'Недозвон', 'Приехал',
        'Уточнить по дате', 'Перезвонить', 'Отказ клиента', 'Продажа за наличные',
        'Продажа в кредит', 'Соскок', 'Консультация', 'Отказ по банкам', 'Одобрен банк',
        'Одобрено банк', 'Новая', 'Заполнить', 'Новая: Не отвечает', 'Т. Кредит',
        'А. Кредит', 'Одобрить', 'Одобрен', 'Отложенный', 'В работе - odobrit',
        'Перезвонить срочно', 'Одобренные', 'Оформленные', 'Одобрение', 'Дошел в КО',
    )
    priezd = (
        'В салоне', 'В салоне не отмечен', 'Купил', 'Приехал', 'Соскок', 'Консультация',
        'Отказ по банкам', 'Одобрен банк', 'Продажа за наличные', 'Продажа в кредит',
        'Одобрить', 'Одобрен', 'На рассмотрении', 'Т. Кредит', 'А. Кредит',
        'Одобренные', 'Оформленные', 'Одобрение', 'Дошел в КО',
    )
    sale = ('Купил', 'Продажа за наличные', 'Продажа в кредит', 'Т. Кредит', 'А. Кредит', 'Оформленные', 'COMPLETED', 'Продажа')
    nekorr = ('Некорректные данные', 'Корзина', 'Повтор', 'Нет данных', 'Дубль', '***', 'Спам', 'Хлам', 'Отбракованные', 'Общие вопросы')
    ne_otvechaet = ('Не отвечает', 'Новая: Не отвечает')
    korr_expr = f"toInt64({_in_status(status_expr, korr)})"
    ne_otvechaet_expr = f"toInt64({_in_status(status_expr, ne_otvechaet)})"
    filtr_expr = f"toInt64(ifNull({status_expr}, '') = 'Фильтр')"
    nedozvon_expr = f"toInt64(ifNull({status_expr}, '') = 'Недозвон')"
    return f"""
        toInt64(if(ifNull({status_expr}, '') != '', 1, 0)) AS kol_vo_zayavok,
        {korr_expr} AS korr,
        {korr_expr} - {ne_otvechaet_expr} - {filtr_expr} - {nedozvon_expr} AS kval,
        toInt64({_in_status(status_expr, priezd)}) AS priezd,
        toInt64({_in_status(status_expr, sale)}) AS prodazhi,
        toInt64({_in_status(status_expr, nekorr)}) AS nekorr,
        {ne_otvechaet_expr} AS ne_otvechaet,
        {filtr_expr} AS filtr,
        {nedozvon_expr} AS nedozvon,
        toInt64(ifNull({status_expr}, '') = 'Приедет') AS priedet,
        toInt64(0) AS dohod_do_kredita,
        toInt64(0) AS dobro
    """


def _gs_metric(column: str) -> str:
    return (
        f"ifNull(toDecimal256OrNull(replaceAll(ifNull(toString(g.`{column}`), ''), ',', '.'), 6), "
        "toDecimal256(0, 6))"
    )


def _api_metric(column: str) -> str:
    return f"toDecimal256(ifNull(t.{column}, 0), 6)"


def _require(client, database: str, table: str) -> None:
    if not table_exists(client, database, table):
        raise RuntimeError(f"{database}.{table} отсутствует")


def _telega_replacements_path() -> Path:
    return Path(__file__).resolve().parents[1] / "step0_sync_local" / "telega_in_orders_replacements.json"


def _ensure_telega_field_overrides(client) -> int:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS ad_analytics.{TELEGA_FIELD_OVERRIDES}
        (
            id Int64,
            post_links Nullable(String),
            utm_source Nullable(String),
            utm_medium Nullable(String),
            utm_campaign Nullable(String),
            utm_content Nullable(String),
            utm_term Nullable(String),
            loaded_at DateTime DEFAULT now()
        )
        ENGINE = ReplacingMergeTree(loaded_at)
        ORDER BY id
        """,
        settings=SAFE_QUERY_SETTINGS,
    )

    path = _telega_replacements_path()
    if not path.exists():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for item in payload.get("replacements", []):
        rows.append(
            (
                int(item["id"]),
                item.get("post_links"),
                item.get("utm_source"),
                item.get("utm_medium"),
                item.get("utm_campaign"),
                item.get("utm_content"),
                item.get("utm_term"),
            )
        )
    client.command(f"TRUNCATE TABLE ad_analytics.{TELEGA_FIELD_OVERRIDES}", settings=SAFE_QUERY_SETTINGS)
    if rows:
        client.insert(
            f"ad_analytics.{TELEGA_FIELD_OVERRIDES}",
            rows,
            column_names=[
                "id",
                "post_links",
                "utm_source",
                "utm_medium",
                "utm_campaign",
                "utm_content",
                "utm_term",
            ],
        )
    return len(rows)


def _ensure_telega_price_overrides(client) -> int:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS ad_analytics.{TELEGA_PRICE_OVERRIDES}
        (
            id Int64,
            total_price Decimal(18, 6),
            source String,
            loaded_at DateTime DEFAULT now()
        )
        ENGINE = ReplacingMergeTree(loaded_at)
        ORDER BY id
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    current_rows = int(
        client.query(
            f"SELECT count() FROM ad_analytics.{TELEGA_PRICE_OVERRIDES}",
            settings=SAFE_QUERY_SETTINGS,
        ).result_rows[0][0]
    )
    if current_rows:
        return current_rows

    if table_exists(client, "ad_analytics", "local_telega_in_orders"):
        legacy_rows = client.query(
            """
            SELECT id, total_price
            FROM ad_analytics.local_telega_in_orders
            WHERE id IS NOT NULL AND total_price IS NOT NULL
            """,
            settings=SAFE_QUERY_SETTINGS,
        ).result_rows
        if legacy_rows:
            client.insert(
                f"ad_analytics.{TELEGA_PRICE_OVERRIDES}",
                [(int(row[0]), Decimal(str(row[1])), "seed_from_ch_legacy_local_telega") for row in legacy_rows],
                column_names=["id", "total_price", "source"],
            )
            return len(legacy_rows)
    return 0


def _rebuild_local_telega_orders(client) -> int:
    _require(client, "raw_data", "telega_in_orders")
    replace_view(
        client,
        "ad_analytics.local_telega_in_orders",
        f"""
        WITH
        field_override AS
        (
            SELECT
                id,
                anyLast(post_links) AS post_links,
                anyLast(utm_source) AS utm_source,
                anyLast(utm_medium) AS utm_medium,
                anyLast(utm_campaign) AS utm_campaign,
                anyLast(utm_content) AS utm_content,
                anyLast(utm_term) AS utm_term
            FROM ad_analytics.{TELEGA_FIELD_OVERRIDES}
            GROUP BY id
        ),
        price_override AS
        (
            SELECT id, argMax(total_price, loaded_at) AS total_price
            FROM ad_analytics.{TELEGA_PRICE_OVERRIDES}
            GROUP BY id
        )
        SELECT
            toInt64(ifNull(r.id, 0)) AS id,
            CAST(r.uid, 'Nullable(String)') AS uid,
            CAST(r.order_id, 'Nullable(Int64)') AS order_id,
            CAST(r.order_project_name, 'Nullable(String)') AS order_project_name,
            CAST(r.order_comment, 'Nullable(String)') AS order_comment,
            CAST(r.channel_id, 'Nullable(Int64)') AS channel_id,
            CAST(r.channel_name, 'Nullable(String)') AS channel_name,
            CAST(r.channel_link, 'Nullable(String)') AS channel_link,
            CAST(r.post_link, 'Nullable(String)') AS post_link,
            CAST(r.placement_format, 'Nullable(String)') AS placement_format,
            CAST(r.status, 'Nullable(String)') AS status,
            CAST(r.cancel_comment, 'Nullable(String)') AS cancel_comment,
            CAST(toDecimal64OrNull(toString(r.price), 6), 'Nullable(Decimal(18, 6))') AS price,
            CAST(coalesce(p.total_price, toDecimal64OrNull(toString(r.price), 6)), 'Nullable(Decimal(18, 6))') AS total_price,
            CAST(r.total_views, 'Nullable(Int64)') AS total_views,
            CAST(r.clicks, 'Nullable(Int64)') AS clicks,
            CAST(coalesce(f.post_links, r.post_links), 'Nullable(String)') AS post_links,
            CAST(coalesce(f.utm_source, r.utm_source), 'Nullable(String)') AS utm_source,
            CAST(coalesce(f.utm_medium, r.utm_medium), 'Nullable(String)') AS utm_medium,
            CAST(coalesce(f.utm_campaign, r.utm_campaign), 'Nullable(String)') AS utm_campaign,
            CAST(coalesce(f.utm_content, r.utm_content), 'Nullable(String)') AS utm_content,
            CAST(coalesce(f.utm_term, r.utm_term), 'Nullable(String)') AS utm_term,
            CAST(parseDateTimeBestEffortOrNull(ifNull(r.created_at, '')), 'Nullable(DateTime)') AS created_at,
            CAST(parseDateTimeBestEffortOrNull(ifNull(r.completed_at, '')), 'Nullable(DateTime)') AS completed_at,
            CAST(parseDateTimeBestEffortOrNull(ifNull(r.done_at, '')), 'Nullable(DateTime)') AS done_at,
            CAST(parseDateTimeBestEffortOrNull(ifNull(r.run_at, '')), 'Nullable(DateTime)') AS run_at,
            CAST(r.raw, 'Nullable(String)') AS raw,
            toDateTime(r.loaded_at) AS updated_at
        FROM raw_data.telega_in_orders r
        LEFT JOIN field_override f ON f.id = toInt64(ifNull(r.id, 0))
        LEFT JOIN price_override p ON p.id = toInt64(ifNull(r.id, 0))
        """,
    )
    return count_rows(client, "ad_analytics.local_telega_in_orders")


def _effective_date_expr(alias: str = "o") -> str:
    return (
        f"coalesce(if(match(ifNull({alias}.utm_content, ''), '^[0-9]{{8}}$'), "
        f"toDateOrNull(concat(substring(ifNull({alias}.utm_content, ''), 5, 4), '-', "
        f"substring(ifNull({alias}.utm_content, ''), 3, 2), '-', substring(ifNull({alias}.utm_content, ''), 1, 2))), NULL), "
        f"toDate({alias}.completed_at), toDate({alias}.done_at), toDate({alias}.created_at))"
    )


def _raw_domain_expr(alias: str = "o") -> str:
    return (
        f"lowerUTF8(trim(coalesce("
        f"nullIf(extract(JSONExtractString(ifNull({alias}.post_links, ''), 1), 'https?://([^/\"?]+)'), ''), "
        f"nullIf(extract(ifNull({alias}.post_link, ''), 'https?://([^/\"?]+)'), '')"
        f"))) "
    )


def _campaign_source_expr(channel_link: str, utm_source: str) -> str:
    return f"""
        replaceRegexpOne(
            multiIf(
                positionCaseInsensitive(ifNull({channel_link}, ''), 't.me/') > 0
                    OR positionCaseInsensitive(ifNull({channel_link}, ''), 'telegram.me/') > 0, 'telegram',
                positionCaseInsensitive(ifNull({channel_link}, ''), 'instagram.com/') > 0, 'instagram',
                positionCaseInsensitive(ifNull({channel_link}, ''), 'vk.com/') > 0, 'VK',
                positionCaseInsensitive(ifNull({channel_link}, ''), 'tiktok.com/') > 0, 'TikTok',
                positionCaseInsensitive(ifNull({channel_link}, ''), 'max.ru/') > 0, 'Max',
                lowerUTF8(ifNull({utm_source}, '')) = 'max', 'Max',
                lowerUTF8(ifNull({utm_source}, '')) = 'telegram', 'telegram',
                ifNull({utm_source}, '')
            ),
            '_tp8$',
            ''
        )
    """


def _rebuild_telega_leads_agg(client) -> int:
    _require(client, "ad_analytics", "raw_leads")
    target = "ad_analytics._tmp_telega_leads_agg"
    metrics = _telega_api_metric_expr("status")
    client.command(f"DROP TABLE IF EXISTS {target} SYNC", settings=SAFE_QUERY_SETTINGS)
    client.command(
        f"""
        CREATE TABLE {target}
        ENGINE = MergeTree
        ORDER BY (
            ifNull(utm_campaign, ''),
            ifNull(lead_utm_content, ''),
            ifNull(lead_domain, ''),
            ifNull(lead_utm_source, ''),
            ifNull(lead_utm_medium, '')
        )
        AS
        WITH lead_scored AS
        (
            SELECT
                utm_campaign,
                leftPad(trim(ifNull(utm_content, '')), 8, '0') AS lead_utm_content,
                lowerUTF8(trim(ifNull(domain, ''))) AS lead_domain,
                lowerUTF8(trim(ifNull(utm_source, ''))) AS lead_utm_source,
                lowerUTF8(trim(ifNull(utm_medium, ''))) AS lead_utm_medium,
                {metrics}
            FROM ad_analytics.raw_leads
            WHERE ifNull(utm_campaign, '') != ''
        )
        SELECT
            utm_campaign,
            lead_utm_content,
            lead_domain,
            lead_utm_source,
            lead_utm_medium,
            toInt64(sum(kol_vo_zayavok)) AS kol_vo_zayavok,
            toInt64(sum(korr)) AS korr,
            toInt64(sum(kval)) AS kval,
            toInt64(sum(priezd)) AS priezd,
            toInt64(sum(prodazhi)) AS prodazhi,
            toInt64(sum(nekorr)) AS nekorr,
            toInt64(sum(ne_otvechaet)) AS ne_otvechaet,
            toInt64(sum(filtr)) AS filtr,
            toInt64(sum(nedozvon)) AS nedozvon,
            toInt64(sum(priedet)) AS priedet,
            toInt64(sum(dohod_do_kredita)) AS dohod_do_kredita,
            toInt64(sum(dobro)) AS dobro
        FROM lead_scored
        GROUP BY utm_campaign, lead_utm_content, lead_domain, lead_utm_source, lead_utm_medium
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    return count_rows(client, target)


def _rebuild_telega_lead_fact(client) -> int:
    _require(client, "ad_analytics", "local_telega_in_orders")
    _require(client, "raw_data", "gsheet_sites")
    leads_agg = _rebuild_telega_leads_agg(client)
    logger.info("  Telega leads aggregate: %d rows", leads_agg)
    shadow = "ad_analytics.crop_targeting_api_telegain_lead_new"
    effective_date = _effective_date_expr("o")
    raw_domain = _raw_domain_expr("o")
    source_expr = _campaign_source_expr("d.channel_link", "d.utm_source")
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC", settings=SAFE_QUERY_SETTINGS)
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(utm_campaign, ''), id)
        AS
        WITH
        tio_raw AS
        (
            SELECT
                o.*,
                {effective_date} AS effective_date,
                {raw_domain} AS raw_domain
            FROM ad_analytics.local_telega_in_orders o
            WHERE ifNull(o.status, '') = 'complete'
        ),
        tio_dated AS
        (
            SELECT
                *,
                multiIf(
                    nullIf(raw_domain, '') IS NOT NULL AND raw_domain NOT IN ('telega.io', 'max.ru', 't.me'),
                    raw_domain,
                    lowerUTF8(trim(arrayElement(splitByChar(' ', ifNull(order_project_name, '')), 1)))
                ) AS effective_domain
            FROM tio_raw
            WHERE effective_date >= toDate('{DATE_FROM}')
        ),
        tio_dedup AS
        (
            SELECT *
            FROM
            (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY
                            effective_domain,
                            ifNull(utm_campaign, ''),
                            leftPad(trim(ifNull(utm_content, '')), 8, '0'),
                            lowerUTF8(trim(ifNull(utm_source, ''))),
                            lowerUTF8(trim(ifNull(utm_medium, ''))),
                            ifNull(channel_link, ''),
                            ifNull(total_price, toDecimal64(0, 6)),
                            effective_date
                        ORDER BY coalesce(completed_at, done_at, created_at) DESC, id DESC
                    ) AS rn
                FROM tio_dated
            )
            WHERE rn = 1
        ),
        gs_domain AS
        (
            SELECT
                lowerUTF8(trim(ifNull(domain, ''))) AS domain_key,
                anyLast(salon) AS salon,
                anyLast(city) AS city,
                anyLast(directologist) AS directologist,
                anyLast(status) AS status,
                anyLast(site_type) AS site_type,
                anyLast(template) AS template,
                anyLast(region) AS region,
                anyLast(direction) AS direction
            FROM reference_data.gsheet_sites
            WHERE ifNull(domain, '') != ''
            GROUP BY domain_key
        )
        SELECT
            toInt32(row_number() OVER (ORDER BY d.effective_date, d.effective_domain, d.id)) AS id,
            CAST(d.effective_date, 'Nullable(Date)') AS `Date`,
            CAST(d.total_price, 'Nullable(Decimal(18, 6))') AS total_cost,
            CAST(d.channel_link, 'Nullable(String)') AS `CampaignName`,
            CAST(d.effective_domain, 'Nullable(String)') AS domain,
            CAST(gs.salon, 'Nullable(String)') AS `салон`,
            CAST(gs.city, 'Nullable(String)') AS `город`,
            CAST({source_expr}, 'Nullable(String)') AS `источник`,
            CAST('Telega IN', 'Nullable(String)') AS `поставщик`,
            CAST(gs.directologist, 'Nullable(String)') AS `специалист`,
            CAST(gs.status, 'Nullable(String)') AS `статус`,
            CAST(gs.site_type, 'Nullable(String)') AS `тип_сайта`,
            CAST(gs.template, 'Nullable(String)') AS `шаблон`,
            CAST(gs.region, 'Nullable(String)') AS `регион`,
            CAST(coalesce(gs.direction, 'Авто'), 'Nullable(String)') AS direction,
            CAST(ifNull(l.kol_vo_zayavok, 0), 'Nullable(Int64)') AS kol_vo_zayavok,
            CAST(ifNull(l.korr, 0), 'Nullable(Int64)') AS korr,
            CAST(ifNull(l.kval, 0), 'Nullable(Int64)') AS kval,
            CAST(ifNull(l.priezd, 0), 'Nullable(Int64)') AS priezd,
            CAST(ifNull(l.prodazhi, 0), 'Nullable(Int64)') AS prodazhi,
            CAST(ifNull(l.nekorr, 0), 'Nullable(Int64)') AS nekorr,
            CAST(ifNull(l.ne_otvechaet, 0), 'Nullable(Int64)') AS ne_otvechaet,
            CAST(ifNull(l.filtr, 0), 'Nullable(Int64)') AS filtr,
            CAST(ifNull(l.nedozvon, 0), 'Nullable(Int64)') AS nedozvon,
            CAST(ifNull(l.priedet, 0), 'Nullable(Int64)') AS priedet,
            CAST(ifNull(l.dohod_do_kredita, 0), 'Nullable(Int64)') AS dohod_do_kredita,
            CAST(ifNull(l.dobro, 0), 'Nullable(Int64)') AS dobro,
            CAST(d.utm_campaign, 'Nullable(String)') AS utm_campaign
        FROM tio_dedup d
        LEFT JOIN gs_domain gs ON gs.domain_key = d.effective_domain
        LEFT JOIN ad_analytics._tmp_telega_leads_agg l
          ON l.utm_campaign = ifNull(d.utm_campaign, '')
         AND l.lead_utm_content = leftPad(trim(ifNull(d.utm_content, '')), 8, '0')
         AND l.lead_domain = d.effective_domain
         AND l.lead_utm_source = lowerUTF8(trim(ifNull(d.utm_source, '')))
         AND l.lead_utm_medium = lowerUTF8(trim(ifNull(d.utm_medium, '')))
        """,
        settings=JOIN_QUERY_SETTINGS,
    )
    swap_shadow(client, "ad_analytics.crop_targeting_api_telegain_lead", shadow)
    client.command("DROP TABLE IF EXISTS ad_analytics._tmp_telega_leads_agg SYNC", settings=SAFE_QUERY_SETTINGS)
    return count_rows(client, "ad_analytics.crop_targeting_api_telegain_lead")


def _rebuild_telega_errors(client) -> int:
    _require(client, "ad_analytics", "local_telega_in_orders")
    _require(client, "raw_data", "gsheet_sites")
    shadow = "ad_analytics.local_telega_in_orders_errors_new"
    effective_date = _effective_date_expr("o")
    raw_domain = _raw_domain_expr("o")
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC", settings=SAFE_QUERY_SETTINGS)
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        ORDER BY (ifNull(id, 0), ifNull(error_type, ''))
        AS
        WITH
        orders AS
        (
            SELECT
                o.*,
                {effective_date} AS effective_date,
                multiIf(
                    nullIf({raw_domain}, '') IS NOT NULL AND {raw_domain} NOT IN ('telega.io', 'max.ru', 't.me'),
                    {raw_domain},
                    lowerUTF8(trim(arrayElement(splitByChar(' ', ifNull(o.order_project_name, '')), 1)))
                ) AS effective_domain,
                leftPad(trim(ifNull(o.utm_content, '')), 8, '0') AS utm_content_norm
            FROM ad_analytics.local_telega_in_orders o
            WHERE ifNull(o.status, '') = 'complete'
              AND o.created_at >= toDateTime('2026-05-01 00:00:00')
        ),
        gs_domain AS
        (
            SELECT
                lowerUTF8(trim(ifNull(domain, ''))) AS domain_key,
                anyLast(status) AS status,
                anyLast(directologist) AS directologist,
                anyLast(salon) AS salon,
                anyLast(city) AS city,
                anyLast(region) AS region
            FROM reference_data.gsheet_sites
            WHERE ifNull(domain, '') != ''
            GROUP BY domain_key
        ),
        error_rows AS
        (
            SELECT
                o.id,
                o.order_id,
                o.order_project_name,
                o.post_links,
                o.status,
                o.utm_source,
                o.utm_medium,
                o.utm_campaign,
                o.utm_content,
                o.utm_content_norm,
                o.effective_domain,
                gs.status AS site_status,
                gs.directologist,
                gs.salon,
                gs.city,
                gs.region,
                o.total_price,
                o.created_at,
                arrayFilter(x -> x.2 != '', [
                    ('неверный utm_content',
                     multiIf(ifNull(o.utm_content, '') = '', 'utm_content пустой/NULL',
                             NOT match(o.utm_content_norm, '^[0-9]{{8}}$'), concat('после lpad не 8 цифр: ', o.utm_content_norm), '')),
                    ('пустой utm_campaign', if(ifNull(trim(o.utm_campaign), '') = '', 'utm_campaign пустой/NULL', '')),
                    ('пустой utm_source', if(ifNull(trim(o.utm_source), '') = '', 'utm_source пустой/NULL', '')),
                    ('пустой utm_medium', if(ifNull(trim(o.utm_medium), '') = '', 'utm_medium пустой/NULL', '')),
                    ('домен не извлекается', if(ifNull(trim(o.effective_domain), '') = '', 'effective_domain пустой/NULL (нет host в post_links и пустой order_project_name)', ''))
                ]) AS errors
            FROM orders o
            LEFT JOIN gs_domain gs ON gs.domain_key = o.effective_domain
        )
        SELECT
            CAST(id, 'Nullable(Int64)') AS id,
            CAST(order_id, 'Nullable(Int64)') AS order_id,
            CAST(order_project_name, 'Nullable(String)') AS order_project_name,
            CAST(post_links, 'Nullable(String)') AS post_links,
            CAST(status, 'Nullable(String)') AS status,
            CAST(utm_source, 'Nullable(String)') AS utm_source,
            CAST(utm_medium, 'Nullable(String)') AS utm_medium,
            CAST(utm_campaign, 'Nullable(String)') AS utm_campaign,
            CAST(utm_content, 'Nullable(String)') AS utm_content,
            CAST(utm_content_norm, 'Nullable(String)') AS utm_content_norm,
            CAST(effective_domain, 'Nullable(String)') AS effective_domain,
            CAST(site_status, 'Nullable(String)') AS site_status,
            CAST(directologist, 'Nullable(String)') AS directologist,
            CAST(salon, 'Nullable(String)') AS salon,
            CAST(city, 'Nullable(String)') AS city,
            CAST(region, 'Nullable(String)') AS region,
            CAST(total_price, 'Nullable(Decimal(18, 6))') AS total_price,
            CAST(created_at, 'Nullable(DateTime)') AS created_at,
            CAST(arrayStringConcat(arrayMap(x -> x.1, errors), '; '), 'Nullable(String)') AS error_type,
            CAST(arrayStringConcat(arrayMap(x -> x.2, errors), '; '), 'Nullable(String)') AS error_detail,
            now() AS checked_at
        FROM error_rows
        WHERE length(errors) > 0
        """,
        settings=JOIN_QUERY_SETTINGS,
    )
    swap_shadow(client, "ad_analytics.local_telega_in_orders_errors", shadow)
    return count_rows(client, "ad_analytics.local_telega_in_orders_errors")


def _rebuild_telega_sources(client) -> tuple[int, int, int, int]:
    field_rows = _ensure_telega_field_overrides(client)
    price_rows = _ensure_telega_price_overrides(client)
    local_rows = _rebuild_local_telega_orders(client)
    lead_rows = _rebuild_telega_lead_fact(client)
    error_rows = _rebuild_telega_errors(client)
    logger.info(
        "  Telega v6 rebuild: field_overrides=%d, price_overrides=%d, local=%d, lead_fact=%d, errors=%d",
        field_rows,
        price_rows,
        local_rows,
        lead_rows,
        error_rows,
    )
    return local_rows, lead_rows, error_rows, price_rows


def _create_empty_overlay(client, shadow: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow} AS ad_analytics.{SOURCE_STORE}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(key3, ''))
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _insert_crop_gsheet_costs(client, target: str) -> None:
    client.command(
        f"""
        INSERT INTO {target}
        WITH
        gs_salon AS
        (
            SELECT
                lowerUTF8(trim(ifNull(salon, ''))) AS salon_key,
                anyLast(project_manager) AS project_manager,
                anyLast(sales_manager) AS sales_manager,
                anyLast(client_id) AS client_id,
                anyLast(site_type) AS site_type,
                anyLast(city) AS city,
                anyLast(region) AS region
            FROM reference_data.gsheet_sites
            WHERE ifNull(salon, '') != ''
            GROUP BY salon_key
        ),
        gs_domain AS
        (
            SELECT
                lowerUTF8(trim(ifNull(domain, ''))) AS domain_key,
                anyLast(site_type) AS site_type,
                anyLast(city) AS city,
                anyLast(region) AS region,
                anyLast(login_key) AS login_key
            FROM reference_data.gsheet_sites
            WHERE ifNull(domain, '') != ''
            GROUP BY domain_key
        ),
        {_crm_by_domain_cte()}
        SELECT
            concat('crop_cost|gs|', toString(g.id)) AS key3,
            {_GS_DATE} AS `Date`,
            multiIf(toDayOfWeek({_GS_DATE}) = 1, '1_Понедельник', toDayOfWeek({_GS_DATE}) = 2, '2_Вторник',
                    toDayOfWeek({_GS_DATE}) = 3, '3_Среда', toDayOfWeek({_GS_DATE}) = 4, '4_Четверг',
                    toDayOfWeek({_GS_DATE}) = 5, '5_Пятница', toDayOfWeek({_GS_DATE}) = 6, '6_Суббота', '7_Воскресенье') AS `День недели`,
            toStartOfWeek({_GS_DATE}, 1) AS week_start,
            toInt64(0) AS `CampaignId`,
            CAST(g.`Канал`, 'Nullable(String)') AS `CampaignName`,
            toInt64(0) AS `AdGroupId`,
            CAST(NULL, 'Nullable(String)') AS `AdGroupName`,
            CAST(NULL, 'Nullable(String)') AS `AdNetworkType`,
            CAST(NULL, 'Nullable(String)') AS `Device`,
            toDecimal64(0, 6) AS `Impressions`,
            toDecimal64(0, 6) AS `Clicks`,
            {_GS_COST} AS total_cost,
            CAST(g.`Сайт`, 'Nullable(String)') AS domain,
            toInt64(0) AS `RlAdjustmentId`,
            '' AS `RlAdjustmentId_total`,
            CAST('Посевы', 'Nullable(String)') AS campaign_code,
            '' AS tp,
            'посевы' AS cpc_cpa,
            'посевы' AS site_quiz,
            CAST(NULL, 'Nullable(String)') AS adgroup_code,
            '' AS account_login,
            CAST('посевы', 'Nullable(String)') AS manager_login,
            '' AS ag_part1, '' AS ag_part2, '' AS ag_part3, '' AS ag_part4, '' AS ag_part5, '' AS ag_part6, '' AS ag_part7,
            '' AS `марки авто`,
            ifNull(nullIf(crm.crm_name, ''), 'Не указана') AS `Название crm`,
            CAST('Заявки', 'Nullable(String)') AS `тип_заявки`,
            {_gs_metric("kol_vo_zayavok")} AS kol_vo_zayavok,
            {_gs_metric("korr")} AS korr,
            {_gs_metric("kval")} AS kval,
            {_gs_metric("priezd")} AS priezd,
            {_gs_metric("prodazhi")} AS prodazhi,
            {_gs_metric("nekorr")} AS nekorr,
            {_gs_metric("ne_otvechaet")} AS ne_otvechaet,
            {_gs_metric("filtr")} AS filtr,
            {_gs_metric("nedozvon")} AS nedozvon,
            {_gs_metric("priedet")} AS priedet,
            toInt64(0) AS dohod_do_kredita,
            toInt64(0) AS dobro,
            CAST(NULL, 'Nullable(String)') AS `статус`,
            CAST(g.`Специалист`, 'Nullable(String)') AS `специалист`,
            CAST(coalesce(gd.site_type, gs.site_type), 'Nullable(String)') AS `тип_сайта`,
            CAST(NULL, 'Nullable(String)') AS `шаблон`,
            CAST(replaceAll(replaceAll(replaceAll(ifNull(g.`Гео`, ''), 'АЦ на Жукова', 'Автоцентр на Жукова'), 'АвтоПарк Южный', 'Автопарк Южный'), 'М-Авто', 'М-авто'), 'Nullable(String)') AS `салон`,
            CAST(coalesce(nullIf(g.`Гео2`, ''), gd.city, gs.city), 'Nullable(String)') AS `город`,
            CAST(coalesce(gd.region, gs.region), 'Nullable(String)') AS `регион`,
            CAST('Авто', 'Nullable(String)') AS direction,
            CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
            CAST(NULL, 'Nullable(String)') AS fid,
            CAST(gs.project_manager, 'Nullable(String)') AS `проджект`,
            CAST(gs.client_id, 'Nullable(String)') AS `id_салона`,
            CAST(gs.sales_manager, 'Nullable(String)') AS `менеджер`,
            {_GS_SOURCE} AS `источник`,
            'Комплекс' AS `направление`,
            '' AS `номер кампании | название кампании`,
            '' AS `номер группы | название группы`,
            CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
            CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
            concat(ifNull(gd.login_key, ''), '|', ifNull(g.`Сайт`, '')) AS `аккаунт|сайт`,
            CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
            CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
            ifNull(nullIf(trim(ifNull(g.`Тип закупа`, '')), ''), 'посевы') AS `поставщик`,
            'crop_targeting' AS _source_table,
            CAST('cost_overlay', 'Nullable(String)') AS cascade_level,
            CAST(NULL, 'Nullable(String)') AS campaign_status,
            CAST(NULL, 'Nullable(String)') AS payment_model
        FROM ad_analytics.gsheets_crop_targeting_account_leads g
        LEFT JOIN gs_salon gs ON gs.salon_key = lowerUTF8(trim(replaceAll(replaceAll(replaceAll(ifNull(g.`Гео`, ''), 'АЦ на Жукова', 'Автоцентр на Жукова'), 'АвтоПарк Южный', 'Автопарк Южный'), 'М-Авто', 'М-авто')))
        LEFT JOIN gs_domain gd ON gd.domain_key = lowerUTF8(trim(ifNull(g.`Сайт`, '')))
        LEFT JOIN crm_by_domain crm ON crm.domain_key = lowerUTF8(trim(ifNull(g.`Сайт`, '')))
        WHERE parseDateTimeBestEffortOrNull(ifNull(g.`Дата`, '')) IS NOT NULL
          AND {_GS_DATE} >= toDate('2026-01-01')
          AND {_GS_DATE} < toDate('2026-05-01')
          AND {_GS_COST} != 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _insert_crop_api_costs(client, target: str) -> None:
    client.command(
        f"""
        INSERT INTO {target}
        WITH gs_salon AS
        (
            SELECT
                lowerUTF8(trim(ifNull(salon, ''))) AS salon_key,
                anyLast(project_manager) AS project_manager,
                anyLast(sales_manager) AS sales_manager,
                anyLast(client_id) AS client_id
            FROM reference_data.gsheet_sites
            WHERE ifNull(salon, '') != ''
            GROUP BY salon_key
        ),
        {_crm_by_domain_cte()}
        SELECT
            concat('crop_cost|api|', toString(t.id)) AS key3,
            assumeNotNull(t.`Date`) AS `Date`,
            multiIf(toDayOfWeek(assumeNotNull(t.`Date`)) = 1, '1_Понедельник', toDayOfWeek(assumeNotNull(t.`Date`)) = 2, '2_Вторник',
                    toDayOfWeek(assumeNotNull(t.`Date`)) = 3, '3_Среда', toDayOfWeek(assumeNotNull(t.`Date`)) = 4, '4_Четверг',
                    toDayOfWeek(assumeNotNull(t.`Date`)) = 5, '5_Пятница', toDayOfWeek(assumeNotNull(t.`Date`)) = 6, '6_Суббота', '7_Воскресенье') AS `День недели`,
            toStartOfWeek(assumeNotNull(t.`Date`), 1) AS week_start,
            toInt64(0) AS `CampaignId`,
            CAST(t.CampaignName, 'Nullable(String)') AS `CampaignName`,
            toInt64(0) AS `AdGroupId`,
            CAST(NULL, 'Nullable(String)') AS `AdGroupName`,
            CAST(NULL, 'Nullable(String)') AS `AdNetworkType`,
            CAST(NULL, 'Nullable(String)') AS `Device`,
            toDecimal64(0, 6) AS `Impressions`,
            toDecimal64(0, 6) AS `Clicks`,
            toDecimal64(ifNull(t.total_cost, toDecimal64(0, 6)), 6) AS total_cost,
            CAST(t.domain, 'Nullable(String)') AS domain,
            toInt64(0) AS `RlAdjustmentId`,
            '' AS `RlAdjustmentId_total`,
            CAST('Посевы', 'Nullable(String)') AS campaign_code,
            '' AS tp,
            'посевы' AS cpc_cpa,
            'посевы' AS site_quiz,
            CAST(NULL, 'Nullable(String)') AS adgroup_code,
            '' AS account_login,
            CAST('посевы', 'Nullable(String)') AS manager_login,
            '' AS ag_part1, '' AS ag_part2, '' AS ag_part3, '' AS ag_part4, '' AS ag_part5, '' AS ag_part6, '' AS ag_part7,
            '' AS `марки авто`,
            ifNull(nullIf(crm.crm_name, ''), 'Не указана') AS `Название crm`,
            CAST('Заявки', 'Nullable(String)') AS `тип_заявки`,
            {_api_metric("kol_vo_zayavok")} AS kol_vo_zayavok,
            {_api_metric("korr")} AS korr,
            {_api_metric("kval")} AS kval,
            {_api_metric("priezd")} AS priezd,
            {_api_metric("prodazhi")} AS prodazhi,
            {_api_metric("nekorr")} AS nekorr,
            {_api_metric("ne_otvechaet")} AS ne_otvechaet,
            {_api_metric("filtr")} AS filtr,
            {_api_metric("nedozvon")} AS nedozvon,
            {_api_metric("priedet")} AS priedet,
            ifNull(t.dohod_do_kredita, 0) AS dohod_do_kredita,
            ifNull(t.dobro, 0) AS dobro,
            CAST(t.`статус`, 'Nullable(String)') AS `статус`,
            CAST(t.`специалист`, 'Nullable(String)') AS `специалист`,
            CAST(t.`тип_сайта`, 'Nullable(String)') AS `тип_сайта`,
            CAST(t.`шаблон`, 'Nullable(String)') AS `шаблон`,
            CAST(t.`салон`, 'Nullable(String)') AS `салон`,
            CAST(t.`город`, 'Nullable(String)') AS `город`,
            CAST(t.`регион`, 'Nullable(String)') AS `регион`,
            CAST(coalesce(t.direction, 'Авто'), 'Nullable(String)') AS direction,
            CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
            CAST(NULL, 'Nullable(String)') AS fid,
            CAST(gs.project_manager, 'Nullable(String)') AS `проджект`,
            CAST(gs.client_id, 'Nullable(String)') AS `id_салона`,
            CAST(gs.sales_manager, 'Nullable(String)') AS `менеджер`,
            {_API_SOURCE} AS `источник`,
            'Комплекс' AS `направление`,
            '' AS `номер кампании | название кампании`,
            '' AS `номер группы | название группы`,
            CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
            CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
            concat('|', ifNull(t.domain, '')) AS `аккаунт|сайт`,
            CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
            CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
            ifNull(nullIf(trim(ifNull(t.`поставщик`, '')), ''), 'посевы') AS `поставщик`,
            'crop_targeting' AS _source_table,
            CAST('cost_overlay', 'Nullable(String)') AS cascade_level,
            CAST(NULL, 'Nullable(String)') AS campaign_status,
            CAST(NULL, 'Nullable(String)') AS payment_model
        FROM ad_analytics.crop_targeting_api_telegain_lead t
        LEFT JOIN gs_salon gs ON gs.salon_key = lowerUTF8(trim(ifNull(t.`салон`, '')))
        LEFT JOIN crm_by_domain crm ON crm.domain_key = lowerUTF8(trim(ifNull(t.domain, '')))
        WHERE t.`Date` >= toDate('2026-05-01')
          AND ifNull(t.total_cost, toDecimal64(0, 6)) != 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _insert_vk_ads_costs(client, target: str) -> None:
    """Строки чистого расхода ВК Рекламы (_source_table='vk_ads') в cost-overlay витрины.

    VK_AUTO_ACCOUNT_SCOPE_2026-08-05: скоуп сужен до Авто-аккаунтов агентства
    (`VK_AUTO_ACCOUNTS_SQL`), как в v5 (там сужение стояло на step0 при наливе
    `local_vk_ads_stats_day`). Без него сюда попадал ВЕСЬ агентский кабинет —
    90 чужих account_id и 13.5 млн ₽ вместо расхода своих Авто-клиентов.
    Зерно (event_date, account_id, ad_plan_id) — как у v5 `vk_ads_by_plan`
    (`work/big_analytics_v5/step3_build_sources/step3.py:2689-2697`), не менялось.
    """
    _require(client, "raw_data", "vk_ads_stats_day")
    _require(client, "raw_data", "vk_ads_agency_clients")
    client.command(
        f"""
        INSERT INTO {target}
        WITH vk_spend AS
        (
            SELECT
                event_date,
                account_id,
                ad_plan_id,
                anyLast(ad_plan_name) AS ad_plan_name,
                sum(spent) AS spent
            FROM
            (
                SELECT
                    toDateOrNull(date) AS event_date,
                    account_id,
                    ad_plan_id,
                    ad_plan_name,
                    ifNull(spent, 0) AS spent
                FROM raw_data.vk_ads_stats_day
                WHERE date >= '{DATE_FROM}'
                  AND ifNull(spent, 0) != 0
                  AND account_id IN ({VK_AUTO_ACCOUNTS_SQL})
            )
            WHERE event_date IS NOT NULL
            GROUP BY event_date, account_id, ad_plan_id
        )
        SELECT
            concat('vk_ads_cost|', toString(event_date), '|', toString(ifNull(account_id, 0)), '|', toString(ifNull(ad_plan_id, 0))) AS key3,
            event_date AS `Date`,
            multiIf(toDayOfWeek(event_date) = 1, '1_Понедельник', toDayOfWeek(event_date) = 2, '2_Вторник',
                    toDayOfWeek(event_date) = 3, '3_Среда', toDayOfWeek(event_date) = 4, '4_Четверг',
                    toDayOfWeek(event_date) = 5, '5_Пятница', toDayOfWeek(event_date) = 6, '6_Суббота', '7_Воскресенье') AS `День недели`,
            toStartOfWeek(event_date, 1) AS week_start,
            toInt64(ifNull(ad_plan_id, 0)) AS `CampaignId`,
            ad_plan_name AS `CampaignName`,
            toInt64(0) AS `AdGroupId`,
            CAST(NULL, 'Nullable(String)') AS `AdGroupName`,
            CAST(NULL, 'Nullable(String)') AS `AdNetworkType`,
            CAST(NULL, 'Nullable(String)') AS `Device`,
            toDecimal64(0, 6) AS `Impressions`,
            toDecimal64(0, 6) AS `Clicks`,
            toDecimal64(spent, 6) AS total_cost,
            CAST(NULL, 'Nullable(String)') AS domain,
            toInt64(0) AS `RlAdjustmentId`,
            '' AS `RlAdjustmentId_total`,
            CAST('VK Ads', 'Nullable(String)') AS campaign_code,
            '' AS tp,
            'cpc' AS cpc_cpa,
            '' AS site_quiz,
            CAST(NULL, 'Nullable(String)') AS adgroup_code,
            '' AS account_login,
            CAST('VK Ads', 'Nullable(String)') AS manager_login,
            '' AS ag_part1, '' AS ag_part2, '' AS ag_part3, '' AS ag_part4, '' AS ag_part5, '' AS ag_part6, '' AS ag_part7,
            '' AS `марки авто`,
            'Не указана' AS `Название crm`,
            CAST('Заявки', 'Nullable(String)') AS `тип_заявки`,
            toDecimal256(0, 6) AS kol_vo_zayavok,
            toDecimal256(0, 6) AS korr,
            toDecimal256(0, 6) AS kval,
            toDecimal256(0, 6) AS priezd,
            toDecimal256(0, 6) AS prodazhi,
            toDecimal256(0, 6) AS nekorr,
            toDecimal256(0, 6) AS ne_otvechaet,
            toDecimal256(0, 6) AS filtr,
            toDecimal256(0, 6) AS nedozvon,
            toDecimal256(0, 6) AS priedet,
            toInt64(0) AS dohod_do_kredita,
            toInt64(0) AS dobro,
            CAST(NULL, 'Nullable(String)') AS `статус`,
            CAST(NULL, 'Nullable(String)') AS `специалист`,
            CAST(NULL, 'Nullable(String)') AS `тип_сайта`,
            CAST(NULL, 'Nullable(String)') AS `шаблон`,
            CAST(NULL, 'Nullable(String)') AS `салон`,
            CAST(NULL, 'Nullable(String)') AS `город`,
            CAST(NULL, 'Nullable(String)') AS `регион`,
            CAST('Авто', 'Nullable(String)') AS direction,
            CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
            CAST(NULL, 'Nullable(String)') AS fid,
            CAST(NULL, 'Nullable(String)') AS `проджект`,
            CAST(NULL, 'Nullable(String)') AS `id_салона`,
            CAST(NULL, 'Nullable(String)') AS `менеджер`,
            'VK Ads' AS `источник`,
            'Комплекс' AS `направление`,
            '' AS `номер кампании | название кампании`,
            '' AS `номер группы | название группы`,
            CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
            CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
            '' AS `аккаунт|сайт`,
            CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
            CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
            'VK Ads' AS `поставщик`,
            'vk_ads' AS _source_table,
            CAST('cost_overlay', 'Nullable(String)') AS cascade_level,
            CAST(NULL, 'Nullable(String)') AS campaign_status,
            CAST(NULL, 'Nullable(String)') AS payment_model
        FROM vk_spend
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _rebuild_cost_overlays(client) -> tuple[int, float]:
    _require(client, "ad_analytics", SOURCE_STORE)
    _require(client, "ad_analytics", "gsheets_crop_targeting_account_leads")
    _rebuild_telega_sources(client)

    shadow = f"ad_analytics.{COST_OVERLAY_TABLE}_new"
    _create_empty_overlay(client, shadow)
    _insert_crop_gsheet_costs(client, shadow)
    _insert_crop_api_costs(client, shadow)
    _insert_vk_ads_costs(client, shadow)
    swap_shadow(client, f"ad_analytics.{COST_OVERLAY_TABLE}", shadow)

    replace_view(
        client,
        "ad_analytics.big_analytics_crop_targeting",
        f"""
        SELECT * FROM ad_analytics.{SOURCE_STORE} WHERE _source_table IN ({CROP_TYPES_SQL})
        UNION ALL
        SELECT * FROM ad_analytics.{COST_OVERLAY_TABLE} WHERE _source_table IN ({CROP_TYPES_SQL})
        """,
    )
    row = client.query(
        f"SELECT count(), toFloat64(sum(total_cost)) FROM ad_analytics.{COST_OVERLAY_TABLE}",
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows[0]
    return int(row[0]), float(row[1] or 0)


def run_crop_phase(client=None) -> dict:
    client = client or get_client()
    overlay_rows, overlay_cost = _rebuild_cost_overlays(client)
    details = f"overlay={overlay_rows:,}, overlay_cost={overlay_cost:,.2f}"
    logger.info("step10a v6_ch завершён: %s", details)
    return {"rows": overlay_rows, "details": details}


def _telega_covered_raw_keys(lo: str, hi: str) -> str:
    effective_date = _effective_date_expr("o")
    raw_domain = _raw_domain_expr("o")
    return f"""
        SELECT l.key3
        FROM ad_analytics.raw_leads l
        INNER JOIN
        (
            SELECT
                ifNull(utm_campaign, '') AS utm_campaign,
                leftPad(trim(ifNull(utm_content, '')), 8, '0') AS utm_content_key,
                lowerUTF8(trim(ifNull(utm_source, ''))) AS utm_source_key,
                lowerUTF8(trim(ifNull(utm_medium, ''))) AS utm_medium_key,
                multiIf(
                    nullIf({raw_domain}, '') IS NOT NULL AND {raw_domain} NOT IN ('telega.io', 'max.ru', 't.me'),
                    {raw_domain},
                    lowerUTF8(trim(arrayElement(splitByChar(' ', ifNull(order_project_name, '')), 1)))
                ) AS domain_key
            FROM ad_analytics.local_telega_in_orders o
            WHERE ifNull(status, '') = 'complete'
              AND {effective_date} >= toDate('2026-05-01')
        ) t
          ON t.utm_campaign = ifNull(l.utm_campaign, '')
         AND t.utm_content_key = leftPad(trim(ifNull(l.utm_content, '')), 8, '0')
         AND t.domain_key = lowerUTF8(trim(ifNull(l.domain, '')))
         AND t.utm_source_key = lowerUTF8(trim(ifNull(l.utm_source, '')))
         AND t.utm_medium_key = lowerUTF8(trim(ifNull(l.utm_medium, '')))
        WHERE l.created_date >= toDate('{lo}')
          AND l.created_date < toDate('{hi}')
          AND ifNull(l.key3, '') != ''
    """


def _overlay_full(client) -> tuple[int, float, float]:
    if not table_exists(client, "ad_analytics", "big_analytics_full"):
        return 0, 0.0, 0.0

    before = client.query(
        """
        SELECT
            toFloat64(sum(total_cost)),
            toFloat64(sum(kol_vo_zayavok) + sum(korr) + sum(kval) + sum(priezd) + sum(prodazhi))
        FROM ad_analytics.big_analytics_full
        """,
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows[0]
    before_cost = float(before[0] or 0)
    before_funnel = float(before[1] or 0)

    shadow = "ad_analytics.big_analytics_full_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        """
        CREATE TABLE ad_analytics.big_analytics_full_new
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(_source_table, ''))
        AS SELECT * FROM ad_analytics.big_analytics_full WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    # Uses global month-safe pipeline batches. Emergency rollback:
    # PIPELINE_BATCH_DAYS=1 python3 pipeline.py --from-step=10.
    full_ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(full_ranges, start=1):
        client.command(
            f"""
            INSERT INTO ad_analytics.big_analytics_full_new
            SELECT *
            FROM ad_analytics.big_analytics_full
            WHERE `Date` >= toDate('{lo}') AND `Date` < toDate('{hi}')
              AND NOT startsWith(key3, 'crop_cost|')
              AND NOT startsWith(key3, 'vk_ads_cost|')
              AND NOT (
                  _source_table IN ('social_посевы', 'telegram')
                  AND key3 IN ({_telega_covered_raw_keys(lo, hi)})
              )
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        logger.info("  full crop-overlay keep batch %d/%d: %s -> %s", idx, len(full_ranges), lo, hi)
    client.command(
        f"""
        INSERT INTO ad_analytics.big_analytics_full_new
        SELECT c.*, CAST(NULL, 'Nullable(String)') AS key_pixel_score
        FROM ad_analytics.{COST_OVERLAY_TABLE} AS c
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    swap_shadow(client, "ad_analytics.big_analytics_full", shadow)

    after = client.query(
        """
        SELECT
            count(),
            toFloat64(sum(total_cost)),
            toFloat64(sum(kol_vo_zayavok) + sum(korr) + sum(kval) + sum(priezd) + sum(prodazhi))
        FROM ad_analytics.big_analytics_full
        """,
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows[0]
    after_rows = int(after[0])
    after_cost = float(after[1] or 0)
    after_funnel = float(after[2] or 0)
    logger.info("  full crop-overlay funnel: %.2f -> %.2f", before_funnel, after_funnel)
    return after_rows, before_cost, after_cost

def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 10 v6_ch: применение стоимости crop/Telega/VK")
    client = get_client()
    t0 = time.perf_counter()
    overlay_rows, overlay_cost = _rebuild_cost_overlays(client)
    full_rows, before_cost, after_cost = _overlay_full(client)
    crop_rows = count_rows(client, "ad_analytics.big_analytics_crop_targeting")
    full_crop_rows = 0
    if table_exists(client, "ad_analytics", "big_analytics_full"):
        full_crop_rows = int(
            client.query(
                """
                SELECT count()
                FROM ad_analytics.big_analytics_full
                WHERE _source_table IN ('crop_targeting', 'crop', 'telegram', 'social_посевы')
                   OR `источник` = 'Посевы'
                """
            ).result_rows[0][0]
        )
    details = (
        f"overlay={overlay_rows:,}, overlay_cost={overlay_cost:,.2f}, "
        f"crop={crop_rows:,}, full_crop_like={full_crop_rows:,}, "
        f"full={full_rows:,}, full_cost={before_cost:,.2f}->{after_cost:,.2f}"
    )
    logger.info("Шаг 10 v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": overlay_rows, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
