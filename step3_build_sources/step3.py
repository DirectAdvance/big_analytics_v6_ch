"""Step 3 for v6_ch: build source marts in ClickHouse.

The v5 module was a large PostgreSQL CTAS/corrections pipeline. This v6 module
materializes the same table names in ClickHouse from `ad_analytics.raw_*` and
`raw_data` reference tables. It intentionally consumes the current ClickHouse
raw_data snapshot; it does not import CRM data.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, day_ranges, q, replace_view, swap_shadow
from config.settings import CDR_PATTERN
from step1_load_raw.step1 import MARCAR_SOURCE_TYPE, MARCAR_STATUS_PRIORITY

logger = logging.getLogger("pipeline.step3")


SOURCE_STORE = "big_analytics_sources"
LEADS_DEDUPED_STAGE = "_step3_leads_deduped"
DIRECT_SOURCE_TYPES = ("direct", "tp8", "tp9", "tp10")
CROP_SOURCE_TYPES = ("crop_targeting", "telegram", "social_посевы", "vk_ads", "vk_zero", "vk_perform")

# source_type (raw_leads/raw_calls/raw_perform_leads) -> ключ `crm` в reference_data.crm_status_mapping.
# Сверено с живой БД 2026-08-05: raw_leads / raw_data.leads_all / raw_calls дают базовые
# source_type, crm_status_mapping — 8 значений crm. Маркер: CRM_MAP_RIVENDELL_2026-08-05.
CRM_BY_SOURCE_TYPE = {
    "crmf_excel": "crmf",
    "genzes_excel": "genzes",
    "marcar_crm_excel": "marcar",
    "mauto_excel": "mauto",
    "mega_crm_excel": "mega",
    "plex_excel": "plex",
    "perform_api": "rivendell",
    "redauto_excel": "redauto",
    "rivendell_excel": "rivendell",
}
CRM_NAME_BY_SOURCE_TYPE = {
    "crmf_excel": "Фаиг",
    "genzes_excel": "Генезис",
    "marcar_crm_excel": "Маркар",
    "mauto_excel": "МаАвто",
    "mega_crm_excel": "Мега",
    "perform_api": "Ривендел",
    "plex_excel": "Плекс",
    "redauto_excel": "Ред Авто",
    "rivendell_excel": "Ривендел",
}
# Фолбэк для source_type, которого нет в словаре: снять суффикс `_excel` / `_crm_excel`.
_CRM_FALLBACK_RE = "(_crm)?_excel$"
STEP3_QUERY_SETTINGS = {
    **SAFE_QUERY_SETTINGS,
    "max_execution_time": 900,
    # MEMORY_LIMIT_2026-08-14: свой лимит 1.5 ГБ убран — общий в SAFE_QUERY_SETTINGS теперь 2 ГБ,
    # и локальное переопределение только опускало бы step3 ниже общего.
}


def _is_transient_clickhouse_error(exc: Exception) -> bool:
    message = str(exc)
    return any(
        marker in message
        for marker in (
            "EOF occurred in violation of protocol",
            "Connection reset by peer",
            "Max retries exceeded",
            "SESSION_IS_LOCKED",
            "Read timed out",
            "RemoteDisconnected",
        )
    )


def _command_with_retry(client, sql: str, *, label: str, settings=None, attempts: int = 3):
    for attempt in range(1, attempts + 1):
        try:
            client.command(sql, settings=settings)
            return client
        except Exception as exc:
            if attempt == attempts or not _is_transient_clickhouse_error(exc):
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


def _weekday_expr(date_expr: str) -> str:
    return (
        f"multiIf(toDayOfWeek({date_expr}) = 1, '1_Понедельник', "
        f"toDayOfWeek({date_expr}) = 2, '2_Вторник', "
        f"toDayOfWeek({date_expr}) = 3, '3_Среда', "
        f"toDayOfWeek({date_expr}) = 4, '4_Четверг', "
        f"toDayOfWeek({date_expr}) = 5, '5_Пятница', "
        f"toDayOfWeek({date_expr}) = 6, '6_Суббота', '7_Воскресенье')"
    )


def _month_plan_expr(date_expr: str, value_expr: str) -> str:
    return (
        f"if(toStartOfMonth({date_expr}) = toStartOfMonth(today()), "
        f"toInt32OrNull(replaceAll(replaceAll(replaceAll(ifNull({value_expr}, ''), '\u00a0', ''), ' ', ''), ',', '.')), NULL)"
    )


def _key_pixel_expr(date_expr: str, domain_expr: str, source_expr: str, campaign_expr: str) -> str:
    return (
        f"concat(ifNull(toString({date_expr}), ''), '|', ifNull({domain_expr}, ''), '|', "
        f"ifNull({source_expr}, ''), '|', ifNull(toString({campaign_expr}), ''))"
    )


def _crm_key(source_type: str) -> str:
    """Python-двойник `_crm_expr` — тот же ключ `crm` для одного source_type."""
    if source_type in CRM_BY_SOURCE_TYPE:
        return CRM_BY_SOURCE_TYPE[source_type]
    return re.sub(_CRM_FALLBACK_RE, "", source_type)


def _crm_expr(source_type_expr: str) -> str:
    """SQL-выражение source_type -> ключ `crm` в reference_data.crm_status_mapping.

    Фолбэк — снятие суффикса, НЕ self-map: `else source_type` возвращал ключ вида
    `rivendell_excel`, которого в crm_status_mapping нет, и вся воронка такой CRM
    молча обнулялась (в CH-маппинге нет general-ветки, в отличие от v5).
    """
    branches = "".join(
        f"{source_type_expr} = '{source_type}', '{crm}', "
        for source_type, crm in sorted(CRM_BY_SOURCE_TYPE.items())
    )
    fallback = f"replaceRegexpOne({source_type_expr}, '{_CRM_FALLBACK_RE}', '')"
    return f"multiIf({branches}{fallback})"


def _crm_name_expr(source_type_expr: str) -> str:
    branches = "".join(
        f"{source_type_expr} = '{source_type}', '{crm_name}', "
        for source_type, crm_name in sorted(CRM_NAME_BY_SOURCE_TYPE.items())
    )
    fallback = f"replaceRegexpOne({source_type_expr}, '{_CRM_FALLBACK_RE}', '')"
    return f"ifNull(nullIf(multiIf({branches}{fallback}), ''), 'Не указана')"


def check_crm_mapping_coverage(client) -> list[str]:
    """Громко сообщить про source_type, чей ключ отсутствует в crm_status_mapping.

    Не роняет шаг: неизвестная CRM не должна останавливать pipeline. Но и молчать
    нельзя — ключ без строк в reference_data.crm_status_mapping обнуляет ВСЮ воронку
    (korr/kval/priezd/prodazhi) этих строк.
    """
    problems: list[str] = []
    try:
        rows = client.query(
            """
            SELECT source_type, sum(n) AS n
            FROM (
                SELECT source_type, count() AS n FROM ad_analytics.raw_leads GROUP BY source_type
                UNION ALL
                SELECT source_type, count() AS n FROM ad_analytics.raw_calls GROUP BY source_type
                UNION ALL
                SELECT source_type, count() AS n FROM ad_analytics.raw_perform_leads GROUP BY source_type
            )
            GROUP BY source_type
            ORDER BY n DESC
            """
        ).result_rows
        mapped = {
            str(row[0])
            for row in client.query("SELECT DISTINCT crm FROM reference_data.crm_status_mapping").result_rows
        }
    except Exception as exc:  # проверка не обязана ронять шаг
        logger.warning("  CRM mapping coverage check пропущен: %s", exc)
        return problems

    for source_type, n in rows:
        source_type = str(source_type or "")
        crm = _crm_key(source_type)
        if source_type not in CRM_BY_SOURCE_TYPE:
            logger.warning(
                "  CRM mapping: неизвестный source_type=%r (%s строк) — ключ выведен фолбэком как %r; "
                "добавьте его в CRM_BY_SOURCE_TYPE (step3.py)",
                source_type,
                f"{int(n):,}",
                crm,
            )
        if crm not in mapped:
            problems.append(f"{source_type}->{crm}:{int(n)}")
            logger.error(
                "  CRM mapping: source_type=%r -> crm=%r ОТСУТСТВУЕТ в reference_data.crm_status_mapping "
                "— воронка (korr/kval/priezd/prodazhi) обнулится для %s строк",
                source_type,
                crm,
                f"{int(n):,}",
            )
    if not problems:
        logger.info("  CRM mapping coverage OK: %d source_type покрыты crm_status_mapping", len(rows))
    return problems


# ══════════════════════════════════════════════════════════════════════════════
# CODE_STATUS_CATEGORY_2026-08-06 — категории статусов, заданные КОДОМ
# ------------------------------------------------------------------------------
# `reference_data.crm_status_mapping` — чужая таблица: у роли пайплайна только
# `SELECT ON raw_data.*`, GRANT на запись не выдаётся (миграция
# `migrations/02_status_mapping_ab_2026-08-05.py` падала `ACCESS_DENIED`).
# Поэтому категории, которые нельзя вписать строкой в справочник, объявляются
# здесь — рядом с остальной логикой категорий (`_category_match_expr`).
#
# Семантика — ОVERRIDE, а не «добавка»: для пары (crm, status) из этого словаря
# строки справочника игнорируются целиком (`(crm, status) NOT IN (...)` во всех
# четырёх ветках `_category_match_expr` И в списках статусов дедупа
# `priezd_statuses`/`sale_statuses`), а категорию задаёт словарь. Источник истины
# для перечисленных пар — ровно он; «половина в справочнике, половина в коде» по
# одной паре невозможна по построению.
#
# Скоуп ровно две CRM, остальные шесть не затрагиваются ни строкой:
#
#   A. MARCAR_GSHEET_STATUS_2026-08-05 — парная половина патча статусов Маркара.
#      `step1_load_raw/step1.py` проставляет лидам Маркара статусы из
#      `MARCAR_STATUS_PRIORITY` по `reference_data.gsheet_priezdi_marcar`; в справочнике
#      из этих четырёх есть только «Приехал», а общей ветки (v5 `crm_name='default'`)
#      в CH-справочнике нет — без пары патч молча обнуляет воронку (priezd ≈ −646,
#      prodazhi −6). Категории сверены с v5 `local_crm_statuses` (замер 2026-08-06):
#      «Продажа»→sale (crm_name='default'), «Дошел в КО»→visit (crm_name=''),
#      «Одобрение»→visit (crm_name=''), «Приехал»→visit (crm_name='default').
#      «Приехал» ПЕРЕНЕСЁН в код вместе с остальными тремя намеренно: иначе одна
#      правка жила бы в двух местах и гард обязан был бы проверять оба. Значение
#      совпадает с тем, что лежит в справочнике сегодня (visit), т.е. перенос —
#      не смена поведения, а сведение четырёх статусов патча в один список.
#
#   B. PLEX_OTKAZ_QUALIFIED_2026-08-05 — «Отказ клиента» у Плекса: correct → qualified
#      (паритет с v5, где статус лежит в общей ветке как qualified). Тот же барьер
#      прав, та же техника. Эквивалентность миграции точная: все 47 строк
#      `plex`/«Отказ клиента» сегодня `correct`, среди них есть строка с `reason=''`,
#      которая уже сейчас матчит пару по первой ветке при ЛЮБОЙ причине, — поэтому
#      override по паре (crm, status) накрывает ровно то же множество лидов.
#      ⚠️ Это ЕДИНСТВЕННАЯ запись здесь, не связанная с патчем в коде: она —
#      временный мост до GRANT'а, а не постоянное место для бизнес-классификации.
#      Появится право на запись — правку B делать в справочнике и удалять отсюда
#      (гард сам скажет, что справочник и код сошлись).
#
# REASON_METRIC_KEY_2026-08-25: reason-метрики (`dohod_do_kredita`/`dobro`) теперь читают
# справочник тем же `_category_match_expr`, что и status-сторона — полным ключом
# (crm, status, salon, reason), а не голым (crm, reason). Поэтому 'credit'/'approved' в
# CODE_STATUS_CATEGORY больше не запрещены (см. `_CODE_STATUS_SIDE_CATEGORIES` ниже).
# ══════════════════════════════════════════════════════════════════════════════
CODE_STATUS_CATEGORY: dict[tuple[str, str], str] = {
    # A. MARCAR_GSHEET_STATUS_2026-08-05 — все четыре статуса патча step1
    ("marcar", "Продажа"): "sale",
    ("marcar", "Дошел в КО"): "credit",      # REASON_METRIC_KEY_2026-08-25: было "visit"
    ("marcar", "Одобрение"): "approved",     # REASON_METRIC_KEY_2026-08-25: было "visit"
    ("marcar", "Приехал"): "visit",
    # B. PLEX_OTKAZ_QUALIFIED_2026-08-05 — временный мост до GRANT'а на справочник
    ("plex", "Отказ клиента"): "qualified",
}

# CASH_SALE_2026-08-25: продажа за наличные не проходит через кредитный отдел —
# в reason-метрики (dohod_do_kredita/dobro) не попадает, в prodazhi остаётся.
CASH_SALE_STATUSES: frozenset[tuple[str, str]] = frozenset({
    ("plex", "Продажа за наличные"),
    ("genzes", "Продажа за наличные"),
})

# PERFORM_STATUS_PARITY_2026-08-14: сайт/v5 считает perform_api по default-ветке
# local_crm_statuses, а не по неполному reference_data.crm_status_mapping.crm='rivendell'.
# В ClickHouse default-CRM нет, поэтому задаём source_type-specific override только
# для perform_api, не меняя реальные rivendell_excel строки.
CODE_SOURCE_STATUS_CATEGORY: dict[tuple[str, str], str] = {
    ("perform_api", "В работе"): "qualified",
    ("perform_api", "В салоне"): "visit",
    ("perform_api", "В салоне не отмечен"): "visit",
    ("perform_api", "Дубль"): "incorrect",
    ("perform_api", "Корзина"): "incorrect",
    ("perform_api", "Купил"): "sale",
    ("perform_api", "Не отвечает"): "correct",
    ("perform_api", "Недозвон"): "correct",
    ("perform_api", "Некорректные данные"): "incorrect",
    ("perform_api", "Новая"): "qualified",
    ("perform_api", "Отказ"): "correct",
    ("perform_api", "Отказ клиента"): "qualified",
    ("perform_api", "Перезвонить"): "qualified",
    ("perform_api", "Перезвонить срочно"): "qualified",
    ("perform_api", "Повтор"): "incorrect",
    ("perform_api", "Приедет"): "qualified",
    ("perform_api", "Приехал"): "visit",
    ("perform_api", "Продажа в кредит"): "sale",
    ("perform_api", "Уточнить по дате"): "qualified",
    ("perform_api", "Фильтр"): "correct",
    ("perform_api", "Хлам"): "incorrect",
}

# Допустимые категории CODE_STATUS_CATEGORY/CODE_SOURCE_STATUS_CATEGORY. 'credit'/'approved'
# добавлены REASON_METRIC_KEY_2026-08-25 — reason-метрики читают тот же код через
# `_category_match_expr`, что и status-сторона (см. комментарий над CODE_STATUS_CATEGORY).
_CODE_STATUS_SIDE_CATEGORIES = frozenset(
    {"correct", "qualified", "visit", "sale", "incorrect", "credit", "approved"})


def _code_pairs(categories: tuple[str, ...] | None = None) -> list[tuple[str, str]]:
    """Пары (crm, status) из `CODE_STATUS_CATEGORY`; None = все."""
    return sorted(
        pair
        for pair, category in CODE_STATUS_CATEGORY.items()
        if categories is None or category in categories
    )


def _code_pairs_sql(pairs: list[tuple[str, str]]) -> str:
    return ", ".join(f"('{crm}', '{status}')" for crm, status in pairs)


def _code_source_pairs(categories: tuple[str, ...] | None = None) -> list[tuple[str, str]]:
    """Пары (source_type, status) из `CODE_SOURCE_STATUS_CATEGORY`; None = все."""
    return sorted(
        pair
        for pair, category in CODE_SOURCE_STATUS_CATEGORY.items()
        if categories is None or category in categories
    )


def _code_source_pairs_sql(pairs: list[tuple[str, str]]) -> str:
    return ", ".join(f"('{source_type}', '{status}')" for source_type, status in pairs)


def _code_override_filter() -> str:
    """` AND (crm, status) NOT IN (...)` — выкинуть из справочника пары, чью категорию задаёт код."""
    pairs_sql = _code_pairs_sql(_code_pairs())
    return f" AND (crm, status) NOT IN ({pairs_sql})" if pairs_sql else ""


def _code_statuses_union_sql(categories: tuple[str, ...], column: str = "status") -> str:
    """`UNION ALL SELECT arrayJoin([...])` — статусы, отнесённые кодом к `categories`."""
    statuses = sorted({status for _crm, status in _code_pairs(categories)})
    if not statuses:
        return ""
    array_sql = ", ".join(f"'{status}'" for status in statuses)
    return f"\n    UNION ALL\n    SELECT arrayJoin([{array_sql}]) AS {column}\n"


def check_code_status_categories(client) -> None:
    """CODE_STATUS_CATEGORY_2026-08-06 — fail-fast на «статус проставляется, а категории нет».

    Раньше (MARCAR_STATUS_GUARD_2026-08-05) гард проверял наличие строк в
    `reference_data.crm_status_mapping` и ронял шаг, пока не применена миграция A. Прав
    на запись в справочник нет и не будет, источник истины для этих статусов —
    `CODE_STATUS_CATEGORY`, поэтому проверяется покрытие КОДОМ:

      1. литералы словаря безопасны для подстановки в SQL;
      2. ключи `crm` — те же, что выдаёт `_crm_expr` (иначе override мёртв);
      3. категории — из допустимого набора `_CODE_STATUS_SIDE_CATEGORIES` (status- и
         reason-сторона читают код одним и тем же `_category_match_expr`);
      4. КАЖДЫЙ статус, который проставляет патч Маркара (`MARCAR_STATUS_PRIORITY`
         в `step1_load_raw/step1.py`), имеет категорию — иначе воронка патченых
         лидов молча обнулится (замер на паритете с v5: priezd ≈ −646, prodazhi −6).

    Пункт 4 — тот же смысл, что у прежнего гарда: добавили пятый статус в патч
    step1 и забыли категорию → шаг 3 падает до тяжёлой работы, а не отдаёт
    молчаливую недостачу приездов.

    ⚠️ `check_crm_mapping_coverage` этого НЕ ловит: он проверяет наличие ключа
    `crm`, а не статусов внутри него, — ключ `marcar` есть всегда.

    Пятый блок — информационный (не роняет шаг): показать, что говорит справочник
    по тем же парам. Когда справочник и код сойдутся, строку из
    `CODE_STATUS_CATEGORY` надо снять — лог скажет об этом явно.
    """
    pairs = _code_pairs()
    for crm, status in pairs:
        if any(bad in crm or bad in status for bad in ("'", "\\")):
            raise RuntimeError(
                f"CODE_STATUS_CATEGORY: ключ ({crm!r}, {status!r}) содержит кавычку/бэкслеш — "
                "он подставляется в SQL литералом, экранирования тут нет"
            )

    known_crm = set(CRM_BY_SOURCE_TYPE.values())
    unknown = sorted({crm for crm, _status in pairs if crm not in known_crm})
    if unknown:
        raise RuntimeError(
            f"CODE_STATUS_CATEGORY: ключи crm {unknown} не встречаются среди значений "
            "CRM_BY_SOURCE_TYPE — `_crm_expr` такой ключ никогда не вернёт, override мёртв"
        )

    bad_categories = sorted(
        {
            category
            for category in [*CODE_STATUS_CATEGORY.values(), *CODE_SOURCE_STATUS_CATEGORY.values()]
            if category not in _CODE_STATUS_SIDE_CATEGORIES
        }
    )
    if bad_categories:
        raise RuntimeError(
            f"CODE_STATUS_CATEGORY: категории {bad_categories} вне допустимого набора "
            f"{sorted(_CODE_STATUS_SIDE_CATEGORIES)}"
        )

    unknown_source_types = sorted(
        {source_type for source_type, _status in _code_source_pairs() if source_type not in CRM_BY_SOURCE_TYPE}
    )
    if unknown_source_types:
        raise RuntimeError(
            f"CODE_SOURCE_STATUS_CATEGORY: source_type {unknown_source_types} отсутствуют в "
            "CRM_BY_SOURCE_TYPE — override никогда не сработает"
        )

    marcar_crm = _crm_key(MARCAR_SOURCE_TYPE)
    missing = [status for status in MARCAR_STATUS_PRIORITY if (marcar_crm, status) not in CODE_STATUS_CATEGORY]
    if missing:
        raise RuntimeError(
            f"CODE_STATUS_CATEGORY: нет категории для статусов {missing} у crm={marcar_crm!r}. "
            "Их проставляет патч Маркара в step1_load_raw/step1.py (MARCAR_STATUS_PRIORITY) — "
            "без категории воронка патченых лидов обнулится молча. Добавьте пару в "
            "CODE_STATUS_CATEGORY (step3_build_sources/step3.py) либо откатите патч в step1."
        )

    logger.info(
        "  Code status categories OK: %d пар (crm, status) и %d пар (source_type, status) заданы кодом, "
        "все %d статуса патча Маркара покрыты",
        len(pairs),
        len(CODE_SOURCE_STATUS_CATEGORY),
        len(MARCAR_STATUS_PRIORITY),
    )

    try:
        rows = client.query(
            f"""
            SELECT crm, status, arraySort(groupUniqArray(category)) AS categories
            FROM reference_data.crm_status_mapping
            WHERE (crm, status) IN ({_code_pairs_sql(pairs)})
            GROUP BY crm, status
            """
        ).result_rows
    except Exception as exc:  # информационный блок не обязан ронять шаг
        logger.warning("  Code status categories: сверка со справочником пропущена: %s", exc)
        return
    in_mapping = {(str(crm), str(status)): list(categories) for crm, status, categories in rows}
    # Статусы патча Маркара остаются в коде даже при совпадении со справочником:
    # все четыре обязаны жить одним списком, иначе гард пришлось бы проверять два места.
    patch_pairs = {(marcar_crm, status) for status in MARCAR_STATUS_PRIORITY}
    for pair in pairs:
        code_category = CODE_STATUS_CATEGORY[pair]
        mapped = in_mapping.get(pair)
        if mapped is None:
            logger.info("    %s/%s: справочник молчит, код задаёт %r", pair[0], pair[1], code_category)
        elif mapped == [code_category] and pair in patch_pairs:
            logger.info(
                "    %s/%s: справочник согласен (%r) — дубль по значению, пара остаётся в коде "
                "вместе с остальными статусами патча",
                pair[0],
                pair[1],
                code_category,
            )
        elif mapped == [code_category]:
            logger.warning(
                "    %s/%s: справочник уже говорит %r — строку можно снять из CODE_STATUS_CATEGORY",
                pair[0],
                pair[1],
                code_category,
            )
        else:
            logger.info(
                "    %s/%s: override, справочник %s -> код %r",
                pair[0],
                pair[1],
                mapped,
                code_category,
            )


def _category_match_expr(
    categories: tuple[str, ...],
    status_expr: str,
    reason_expr: str,
    source_type_expr: str,
    salon_expr: str,
) -> str:
    cats_sql = ", ".join(f"'{category}'" for category in categories)
    crm = _crm_expr(source_type_expr)
    status = f"ifNull({status_expr}, '')"
    source_type = f"ifNull({source_type_expr}, '')"
    reason = f"lower(ifNull({reason_expr}, ''))"
    salon = f"lower(trim(ifNull({salon_expr}, '')))"
    # CODE_STATUS_CATEGORY_2026-08-06: пары из словаря выкинуты из справочника
    # (override, а не добавка) и матчатся отдельной веткой по своей категории.
    override = _code_override_filter()
    code_pairs_sql = _code_pairs_sql(_code_pairs(categories))
    code_branch = f"OR ({crm}, {status}) IN ({code_pairs_sql})" if code_pairs_sql else ""
    code_source_pairs_sql = _code_source_pairs_sql(_code_source_pairs(categories))
    code_source_branch = (
        f"OR ({source_type}, {status}) IN ({code_source_pairs_sql})" if code_source_pairs_sql else ""
    )
    return f"""
    (
        ({crm}, {status}) IN (
            SELECT crm, status FROM reference_data.crm_status_mapping
            WHERE category IN ({cats_sql}) AND reason = '' AND salon = ''{override}
        )
        OR ({crm}, {status}, {reason}) IN (
            SELECT crm, status, lower(reason) FROM reference_data.crm_status_mapping
            WHERE category IN ({cats_sql}) AND reason != '' AND salon = ''{override}
        )
        OR ({crm}, {salon}, {status}) IN (
            SELECT crm, lower(salon), status FROM reference_data.crm_status_mapping
            WHERE category IN ({cats_sql}) AND reason = '' AND salon != ''{override}
        )
        OR ({crm}, {salon}, {status}, {reason}) IN (
            SELECT crm, lower(salon), status, lower(reason) FROM reference_data.crm_status_mapping
            WHERE category IN ({cats_sql}) AND reason != '' AND salon != ''{override}
        )
        {code_branch}
        {code_source_branch}
    )
    """


def _metric_expr(status_expr: str, reason_expr: str, source_type_expr: str, salon_expr: str) -> str:
    status = f"ifNull({status_expr}, '')"
    crm = _crm_expr(source_type_expr)
    correct = _category_match_expr(
        ("correct", "qualified", "visit", "sale", "credit", "approved"),
        status_expr,
        reason_expr,
        source_type_expr,
        salon_expr,
    )
    qualified = _category_match_expr(
        ("qualified", "visit", "sale", "credit", "approved"),
        status_expr,
        reason_expr,
        source_type_expr,
        salon_expr,
    )
    visit = _category_match_expr(
        ("visit", "sale", "credit", "approved"),
        status_expr,
        reason_expr,
        source_type_expr,
        salon_expr,
    )
    sale = _category_match_expr(("sale",), status_expr, reason_expr, source_type_expr, salon_expr)
    incorrect = _category_match_expr(("incorrect",), status_expr, reason_expr, source_type_expr, salon_expr)
    # REASON_METRIC_KEY_2026-08-25: dohod_do_kredita/dobro раньше матчились только по
    # (crm, reason), отбрасывая status и salon — та же пара (crm, reason) в справочнике
    # относится к разным категориям в зависимости от status/salon, из-за чего лиды
    # incorrect/correct/visit считались в KO/dobro. Теперь оба используют тот же
    # `_category_match_expr`, что и status-сторона (полный ключ crm/status/salon/reason),
    # плюс sale — FUNNEL.md требует sale ⊆ approved ⊆ credit на reason-стороне.
    cash_sale = f"({crm}, {status}) IN ({_code_pairs_sql(sorted(CASH_SALE_STATUSES))})"
    credit_side = _category_match_expr(
        ("credit", "approved", "sale"), status_expr, reason_expr, source_type_expr, salon_expr
    )
    approved_side = _category_match_expr(
        ("approved", "sale"), status_expr, reason_expr, source_type_expr, salon_expr
    )
    return f"""
    toDecimal64(if({status} != '', 1, 0), 6) AS kol_vo_zayavok,
    toDecimal64(if({correct}, 1, 0), 6) AS korr,
    toDecimal64(if({qualified}, 1, 0), 6) AS kval,
    toDecimal64(if({visit}, 1, 0), 6) AS priezd,
    toDecimal64(if({sale}, 1, 0), 6) AS prodazhi,
    toDecimal64(if({incorrect}, 1, 0), 6) AS nekorr,
    toDecimal64(if({status} IN ('Не отвечает', 'Новая: Не отвечает'), 1, 0), 6) AS ne_otvechaet,
    toDecimal64(if({status} = 'Фильтр', 1, 0), 6) AS filtr,
    toDecimal64(if({status} = 'Недозвон', 1, 0), 6) AS nedozvon,
    toDecimal64(if({status} = 'Приедет', 1, 0), 6) AS priedet,
    toInt64(if({credit_side} AND NOT {cash_sale}, 1, 0)) AS dohod_do_kredita,
    toInt64(if({approved_side} AND NOT {cash_sale}, 1, 0)) AS dobro
"""


def _gs_account_cte() -> str:
    return """
gs_account AS
(
    SELECT
        login_key_norm AS login_key,
        anyLast(domain) AS domain,
        anyLast(status) AS status,
        anyLast(directologist) AS directologist,
        anyLast(site_type) AS site_type,
        anyLast(template) AS template,
        anyLast(salon) AS salon,
        anyLast(city) AS city,
        anyLast(region) AS region,
        anyLast(direction) AS direction,
        anyLast(direction_main) AS direction_main,
        anyLast(project_manager) AS project_manager,
        anyLast(client_id) AS client_id,
        anyLast(sales_manager) AS sales_manager
    FROM
    (
        SELECT *, lower(ifNull(login_key, '')) AS login_key_norm
        FROM reference_data.gsheet_sites
        WHERE ifNull(login_key, '') != ''
    )
    GROUP BY login_key_norm
),
gs_domain AS
(
    SELECT
        domain_key_norm AS domain_key,
        anyLast(domain) AS domain,
        anyLast(status) AS status,
        anyLast(directologist) AS directologist,
        anyLast(site_type) AS site_type,
        anyLast(template) AS template,
        anyLast(salon) AS salon,
        anyLast(city) AS city,
        anyLast(region) AS region,
        anyLast(direction) AS direction,
        anyLast(direction_main) AS direction_main,
        anyLast(project_manager) AS project_manager,
        anyLast(client_id) AS client_id,
        anyLast(sales_manager) AS sales_manager,
        anyLast(login_key) AS login_key
    FROM
    (
        SELECT *, lower(trim(ifNull(domain, ''))) AS domain_key_norm
        FROM reference_data.gsheet_sites
        WHERE ifNull(domain, '') != ''
    )
    GROUP BY domain_key_norm
),
crm_by_domain AS
(
    SELECT
        domain_key_norm AS domain_key,
        multiIf(
            has(groupArray(source_type), 'marcar_crm_excel'), 'Маркар',
            has(groupArray(source_type), 'mega_crm_excel'), 'Мега',
            has(groupArray(source_type), 'crmf_excel'), 'Фаиг',
            has(groupArray(source_type), 'plex_excel'), 'Плекс',
            has(groupArray(source_type), 'redauto_excel'), 'Ред Авто',
            has(groupArray(source_type), 'genzes_excel'), 'Генезис',
            has(groupArray(source_type), 'mauto_excel'), 'МаАвто',
            ifNull(nullIf(anyLast(source_type), ''), 'Не указана')
        ) AS crm_name
    FROM
    (
        SELECT lower(trim(ifNull(domain, ''))) AS domain_key_norm, source_type FROM ad_analytics.raw_leads
        UNION ALL
        SELECT lower(trim(ifNull(domain, ''))) AS domain_key_norm, source_type FROM ad_analytics.raw_calls
    )
    WHERE domain_key_norm != ''
    GROUP BY domain_key_norm
)
"""


# ══════════════════════════════════════════════════════════════════════════════
# PHONE_NORMALIZE_DEDUP_FIX + VISIT_DUP_DEDUP (порт v5 step3.py:93-133)
# ------------------------------------------------------------------------------
# 1. Ключ дедупа — НОРМАЛИЗОВАННЫЙ телефон (последние 10 цифр), а не голая
#    строка `phone`. В v5 это лечило CRM-батч, отдающий телефоны с ведущей «7»
#    (79321286162) вперемешку с форматом без неё (9614572141): точное сравнение
#    не видело один и тот же лид как дубль. ⚠️ В v6 замерено: ВСЕ телефоны
#    `raw_data.leads_all` уже нормализованы (11 цифр, только цифры), поэтому
#    сегодня выражение — тождество (uniqExact по сырому и по нормализованному
#    ключу совпадает до строки). Ставится как ГАРАНТИЯ на будущий батч в другом
#    формате, а не ради текущей дельты.
#
# 2. `_rnv` — дедуп ВНУТРИ одного визита. Ветка `_hp = 1` оставляет ВСЕ
#    visit/sale-строки партиции (лид может реально приезжать несколько раз);
#    побочный эффект — один и тот же визит, выгруженный CRM дважды, выживает
#    дважды и задваивает приезд. Разделитель «реальный повторный визит» vs
#    «дубль выгрузки» — `arrival_date`: ключ визита = (норм. телефон, yclid,
#    arrival_date). Порядок победителя внутри визита: visit/sale-статус → sale
#    вперёд visit → строка с непустым reason (кормит dohod_do_kredita/dobro) →
#    created_date, id (детерминизм).
#    ⚠️ Область действия обоснования (замер 2026-08-06): дедуп отбрасывает 9 396
#    строк, из них 7 849 (83.5%) имеют `arrival_date IS NULL` и попадают в ОДНУ
#    NULL-партицию — для них ключ визита вырождается в (телефон, yclid), то есть
#    к ним аргумент «98% групп имеют один created_date, значит это дубль выгрузки»
#    НЕ применим: там схлопывается вся visit/sale-история лида без даты визита,
#    а не повторная выгрузка одного визита. Порт дословный (v5 step3.py:164-166),
#    поведение v5 воспроизведено 1:1; менять форму ключа здесь без сверки с v5
#    нельзя — это разъедет приезды между контурами.
# ══════════════════════════════════════════════════════════════════════════════

# Нормализованный телефон: только цифры, последние 10 (тот же паттерн, что v5).
_PHONE_NORM_EXPR = "right(replaceRegexpAll(ifNull(phone, ''), '[^0-9]', ''), 10)"


def _leads_deduped_cte() -> str:
    phone = _PHONE_NORM_EXPR
    return f"""
perform_domains AS
(
    SELECT lowerUTF8(trim(ifNull(domain, ''))) AS domain
    FROM reference_data.gsheet_sites
    WHERE client_id = 'avto_0415'
      AND ifNull(domain, '') != ''
),
-- CODE_STATUS_CATEGORY_2026-08-06: списки статусов дедупа читают ТОТ ЖЕ источник
-- истины, что и `_category_match_expr` — иначе патченый статус Маркара считался бы
-- приездом в воронке, но не в ранжировании дедупа, и строка могла бы не выжить.
-- Списки по `status` без разреза crm — как в v5 (`config/status_sql.py::_priezd_statuses_in`).
priezd_statuses AS
(
    SELECT DISTINCT status
    FROM reference_data.crm_status_mapping
    WHERE category IN ('visit', 'sale', 'credit', 'approved')
      AND ifNull(status, '') != ''{_code_override_filter()}{_code_statuses_union_sql(("visit", "sale", "credit", "approved"))}),
sale_statuses AS
(
    SELECT DISTINCT status
    FROM reference_data.crm_status_mapping
    WHERE category = 'sale'
      AND ifNull(status, '') != ''{_code_override_filter()}{_code_statuses_union_sql(("sale",))}),
all_leads AS
(
    SELECT *
    FROM ad_analytics.raw_leads
    WHERE lowerUTF8(trim(ifNull(domain, ''))) NOT IN (SELECT domain FROM perform_domains)
    UNION ALL
    SELECT *
    FROM ad_analytics.raw_perform_leads
),
ranked_leads AS
(
    SELECT
        *,
        max(if(status IN (SELECT status FROM priezd_statuses), 1, 0))
            OVER (PARTITION BY {phone}, yclid) AS _hp,
        row_number() OVER (
            PARTITION BY {phone}, yclid
            ORDER BY if(status IN (SELECT status FROM priezd_statuses), 0, 1), created_date
        ) AS _rn,
        row_number() OVER (
            PARTITION BY {phone}, yclid, arrival_date
            ORDER BY
                if(status IN (SELECT status FROM priezd_statuses), 0, 1),
                if(status IN (SELECT status FROM sale_statuses), 0, 1),
                if(trim(ifNull(reason, '')) != '', 0, 1),
                created_date,
                id
        ) AS _rnv
    FROM all_leads
    WHERE ifNull(phone, '') != ''
      AND ifNull(yclid, '') != ''
),
leads_deduped AS
(
    SELECT * EXCEPT(_hp, _rn, _rnv)
    FROM all_leads
    WHERE ifNull(phone, '') = ''
       OR ifNull(yclid, '') = ''
    UNION ALL
    SELECT * EXCEPT(_hp, _rn, _rnv)
    FROM ranked_leads
    WHERE (_hp = 1 AND status IN (SELECT status FROM priezd_statuses) AND _rnv = 1)
       OR (_hp = 0 AND _rn = 1)
)
"""


def _rebuild_leads_deduped_stage(client):
    target = f"ad_analytics.{LEADS_DEDUPED_STAGE}"
    client = _command_with_retry(client, f"DROP TABLE IF EXISTS {target} SYNC", label=f"{LEADS_DEDUPED_STAGE} drop")
    client = _command_with_retry(
        client,
        f"""
        CREATE TABLE {target}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(created_date, toDate('2026-01-01')))
        ORDER BY (ifNull(created_date, toDate('2026-01-01')), ifNull(key3, ''), id)
        AS
        WITH
        {_leads_deduped_cte()}
        SELECT *
        FROM leads_deduped
        WHERE ifNull(created_date, toDate('1970-01-01')) >= toDate('2026-01-01')
        """,
        label=f"{LEADS_DEDUPED_STAGE} create",
        settings=STEP3_QUERY_SETTINGS,
    )
    return client, count_rows(client, target)


def _ag_part_exprs(prefix: str = "") -> list[str]:
    ag = f"{prefix}adgroup_code"
    return [f"splitByChar('_', ifNull({ag}, ''))[{idx}]" for idx in range(1, 8)]


def _ag_parts_expr(prefix: str = "") -> str:
    parts = ",\n    ".join(f"{expr} AS ag_part{idx}" for idx, expr in enumerate(_ag_part_exprs(prefix), start=1))
    return f"\n    {parts}\n"


def _direct_napravlenie_expr(prefix: str = "yd.") -> str:
    """`направление` строки Директа: v5/site taxonomy keeps Direct inside `Комплекс`."""
    return "'Комплекс'"


def _direct_source_table_expr(prefix: str = "yd.") -> str:
    """`_source_table` строки Директа — v6-эквивалент v5 `_move_tp8_to_crop()`."""
    tp = f"ifNull({prefix}tp, '')"
    return (
        f"if(startsWith({tp}, 'tp8'), 'tp8', "
        f"if(startsWith({tp}, 'tp9'), 'tp9', "
        f"if(startsWith({tp}, 'tp10'), 'tp10', 'direct')))"
    )


def _direct_source_expr(prefix: str = "yd.") -> str:
    """`источник` строки Директа для посевных tp-кампаний."""
    tp = f"ifNull({prefix}tp, '')"
    return (
        f"if(startsWith({tp}, 'tp8'), 'Посевы_Telegram', "
        f"if(startsWith({tp}, 'tp9'), 'Посевы_Max', "
        f"if(startsWith({tp}, 'tp10'), 'Посевы_Telegram+Max', 'Контекст')))"
    )


# Универс посевов — общее определение для ветки crop и для гейта посевной активности
# (POSEVY_MIXED_DOMAIN_ROUTING_FIX ниже). Два разных списка utm молча разошлись бы.
_CROP_UTM_FILTER = """
(
    ifNull(utm_source, '') IN ('telegram','stories_tg','vk_storis','telegram_storis','max','vk','vk_groups','vkads')
    OR ifNull(utm_medium, '') IN ('posev','paid_social')
)
"""


def _perform_vk_filter(prefix: str = "") -> str:
    """Перформ-заявки из VK Ads: в BA5/site живут отдельным источником `VK Ads`."""
    p = prefix
    return (
        f"ifNull({p}source_type, '') = 'crmf_excel' "
        f"AND ifNull({p}utm_source, '') = 'vkads' "
        f"AND ifNull({p}utm_campaign, '') = 'victory'"
    )


def _crop_source_filter() -> str:
    return f"""
{_CROP_UTM_FILTER.strip()}
AND NOT ({_perform_vk_filter()})
"""


# ══════════════════════════════════════════════════════════════════════════════
# POSEVY_MIXED_DOMAIN_ROUTING_FIX (порт v5 step6_build_full/step6.py:741-775)
# ------------------------------------------------------------------------------
# Лид без матча Директ-кампании (direct_zero / direct_unmatched) на СМЕШАННОМ
# посевном домене (`gsheet_sites.direction_main='Посевы'`) в v5 красится в
# `источник='Посевы_Telegram'`, иначе весь посевной трафик такого домена уезжает
# в 'Контекст' — срез по каналу смещается. Подтип канала (Telegram/Max/VK) для
# этой ветки неизвестен (ни tp, ни utm-канал сюда не долетают), поэтому берётся
# тот же дефолт, что и у step10 для неопределённого канала посевов — Telegram.
#
# BA5-факт на 2026-01..08 показывает, что repaint живёт по доменному справочнику
# `direction_main='Посевы'`: старый active-gate оставлял часть BA5-посевных доменов
# в `Контекст`/`SEO`. Поэтому CTE ниже намеренно не зависит от текущей crop-активности.
# ══════════════════════════════════════════════════════════════════════════════

_POSEV_REPAINT_PREDICATE = "l.domain_key IN (SELECT domain_key FROM posev_repaint_domains)"


def _posev_repaint_cte() -> str:
    return """
posev_repaint_domains AS
(
    SELECT DISTINCT lower(trim(ifNull(gs.domain, ''))) AS domain_key
    FROM reference_data.gsheet_sites gs
    WHERE gs.direction_main = 'Посевы'
      AND ifNull(gs.domain, '') != ''
)"""


def _unmatched_source_expr() -> str:
    """`источник` ветки direct_unmatched — порт v5 step3.py:1090-1096 + repaint 6a3.

    В BA5 repaint 6a3 идёт до fallback-заполнения `Контекст`/`SEO`, поэтому посевной
    домен должен выигрывать у статуса сайта.
    """
    return f"""multiIf(
        {_POSEV_REPAINT_PREDICATE}, 'Посевы_Telegram',
        gs.status = 'Контекст активно', 'Контекст',
        gs.status = 'SEO', 'SEO',
        gs.status = 'SEO Flow', 'SEO Flow',
        'Контекст')"""


def _gs_pick_expr(field: str) -> str:
    return (
        f"if(la.domain_key != '' AND la.domain_key != lower(trim(ifNull(gs.domain, ''))), "
        f"coalesce(gs_dir.{field}, gs.{field}), coalesce(gs.{field}, gs_dir.{field}))"
    )


def _direct_specialist_expr() -> str:
    return _domain_specialist_expr("gs")


def _domain_specialist_expr(alias: str) -> str:
    return (
        f"if({alias}.match_priority IN (1, 2), "
        f"nullIf(trim(ifNull({alias}.directologist, '')), ''), "
        "CAST(NULL, 'Nullable(String)'))"
    )


# DIRECT_LEAD_BRANCHES_2026-08-05
# key3 лида = lower(created_date|campaign_id|group_id|device|correction_id) (step1.py:225-238).
# Лид без campaign_id/группы/девайса/корректировки даёт хвост '|0|0|0|0' — в статистике Директа
# такого ключа не бывает (замер: 0 строк raw_yandex с этим хвостом), значит ветка direct_zero
# пересекаться с основной веткой direct не может по построению.
_ZERO_KEY3_PATTERN = "%|0|0|0|0"


def _direct_lead_universe_filter(prefix: str = "") -> str:
    """Единый предикат «лид принадлежит Директу» — не SEO, не пиксель, не соц.посевы.

    Используется И основной веткой (`lead_scored` в `_build_direct_sql`), И ветками
    direct_cascade / direct_unmatched / direct_zero. Одно определение на четыре ветки —
    иначе разъезд фильтров даст либо потерю лидов, либо двойной учёт.

    DIRECT_CROP_DISJOINT_2026-08-05: исключение по `utm_medium` — вторая половина
    дизъюнктности с `_build_crop_sql_batched`. Тот ловит посев ДВУМЯ условиями
    (`utm_source IN (…соц…)` OR `utm_medium IN ('posev','paid_social')`), а здесь
    исключался только `utm_source` — 562 лида 2026 попадали в ОБА универса
    одновременно. Сегодня они не задваиваются лишь потому, что их домены не
    проходят гейт `gs.direction='Авто'` у lead-веток Директа; у crop-ветки такого
    гейта нет, поэтому смена `direction` одного домена на 'Авто' немедленно дала бы
    двойной учёт лида. Ср. v5 step3.py:2744 — там случай исключён явно.
    """
    p = prefix
    return f"""
    ifNull({p}key3, '') != ''
      AND NOT (ifNull({p}utm_source, '') = '' OR ({p}utm_source = 'seo' AND {p}utm_medium = 'organic'))
      AND ifNull({p}utm_source, '') NOT LIKE 'victory_%'
      AND ifNull({p}utm_source, '') NOT IN ('telegram','stories_tg','vk_storis','telegram_storis','max','vk','vk_groups','vkads')
      AND ifNull({p}utm_medium, '') NOT IN ('posev','paid_social')
"""


def _lead_date_filter(lo: str, hi: str) -> str:
    return f"AND created_date >= toDate('{lo}') AND created_date < toDate('{hi}')"


def _zvonki_cdr_expr(utm_content_expr: str) -> str:
    return f"match(lowerUTF8(ifNull({utm_content_expr}, '')), '{CDR_PATTERN}')"


def _claim_type_expr(utm_content_expr: str, deal_type_expr: str) -> str:
    return (
        f"if({_zvonki_cdr_expr(utm_content_expr)}, "
        f"'Звонки_CDR', if(ifNull({deal_type_expr}, '') = '', 'Заявка', {deal_type_expr}))"
    )


def _yd_agg_cte(raw_date_filter: str = "") -> str:
    """CTE `yd` — статистика Директа, свёрнутая по key3.

    Вынесено из `_build_direct_sql`, чтобы каскадная ветка (`_build_direct_cascade_sql`)
    матчилась ровно к тому же агрегату, что и основная ветка. Два определения одного
    агрегата разъехались бы (v5 держит один `yd_agg` на обе ветки).
    """
    return f"""
yd AS
(
    SELECT
        key3,
        max(`Date`) AS `Date`,
        anyLast(`CampaignId`) AS `CampaignId`,
        anyLast(`CampaignName`) AS `CampaignName`,
        anyLast(`AdGroupId`) AS `AdGroupId`,
        anyLast(`AdGroupName`) AS `AdGroupName`,
        anyLast(`AdNetworkType`) AS `AdNetworkType`,
        anyLast(`Device`) AS `Device`,
        sum(`Impressions`) AS `Impressions`,
        sum(`Clicks`) AS `Clicks`,
        sum(total_cost) AS total_cost,
        anyLast(`RlAdjustmentId`) AS `RlAdjustmentId`,
        anyLast(week_start) AS week_start,
        lower(anyLast(account_login)) AS account_login,
        anyLast(manager_login) AS manager_login,
        anyLast(campaign_code) AS campaign_code,
        anyLast(tp) AS tp,
        anyLast(cpc_cpa) AS cpc_cpa,
        anyLast(site_quiz) AS site_quiz,
        anyLast(adgroup_code) AS adgroup_code
    FROM ad_analytics.raw_yandex AS ry
    WHERE 1 = 1
      {raw_date_filter}
    GROUP BY key3
)
"""


def _build_direct_sql(target_table: str, raw_date_filter: str = "") -> str:
    return f"""
CREATE TABLE ad_analytics.{target_table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(`Date`)
ORDER BY (`Date`, ifNull(`CampaignId`, 0), ifNull(key3, ''))
AS
WITH
{_gs_account_cte()},
{_yd_agg_cte(raw_date_filter).strip()},
gs_best AS
(
    SELECT *
    FROM
    (
        SELECT
            ud.login_key AS match_login_key,
            ud.date_val AS match_date,
            gs.domain,
            gs.status,
            gs.directologist,
            gs.site_type,
            gs.template,
            gs.salon,
            gs.city,
            gs.region,
            gs.direction,
            gs.direction_main,
            gs.project_manager,
            gs.client_id,
            gs.sales_manager,
            gs.login_key,
            multiIf(
                ifNull(gs.login_key, '') = '', 99,
                ifNull(trim(gs.launch_date), '') = '' AND ifNull(trim(gs.block_date), '') = '', 2,
                (ifNull(trim(gs.launch_date), '') = '' OR ud.date_val >= toDate(parseDateTimeBestEffortOrNull(gs.launch_date)))
                    AND (ifNull(trim(gs.block_date), '') = '' OR ud.date_val < toDate(parseDateTimeBestEffortOrNull(gs.block_date))),
                1,
                3
            ) AS match_priority,
            row_number() OVER (
                PARTITION BY ud.login_key, ud.date_val
                ORDER BY
                    match_priority ASC,
                    ifNull(toDate(parseDateTimeBestEffortOrNull(gs.launch_date)), toDate('1900-01-01')) DESC,
                    ifNull(gs.domain, '') ASC
            ) AS rn
        FROM
        (
            SELECT DISTINCT account_login AS login_key, `Date` AS date_val
            FROM yd
        ) ud
        LEFT JOIN reference_data.gsheet_sites gs
          ON lower(trim(ifNull(gs.login_key, ''))) = ud.login_key
    )
    WHERE rn = 1
),
lead_scored AS
(
    SELECT
        key3,
        lower(trim(ifNull(domain, ''))) AS domain_key,
        domain AS domain,
        fid AS fid,
        {_crm_name_expr("source_type")} AS crm_name,
        {_zvonki_cdr_expr("utm_content")} AS zvonki_cdr,
        {_metric_expr("status", "reason", "source_type", "salon")}
    FROM ad_analytics.{LEADS_DEDUPED_STAGE}
    WHERE {_direct_lead_universe_filter()}
),
la AS
(
    SELECT
        key3,
        anyLast(domain_key) AS domain_key,
        anyLast(domain) AS domain,
        anyLast(fid) AS fid,
        anyLast(crm_name) AS crm_name,
        zvonki_cdr,
        sum(kol_vo_zayavok) AS kol_vo_zayavok,
        sum(korr) AS korr,
        sum(kval) AS kval,
        sum(priezd) AS priezd,
        sum(prodazhi) AS prodazhi,
        sum(nekorr) AS nekorr,
        sum(ne_otvechaet) AS ne_otvechaet,
        sum(filtr) AS filtr,
        sum(nedozvon) AS nedozvon,
        sum(priedet) AS priedet,
        sum(dohod_do_kredita) AS dohod_do_kredita,
        sum(dobro) AS dobro
    FROM lead_scored
    GROUP BY key3, zvonki_cdr
)
SELECT
    yd.key3 AS key3,
    yd.`Date` AS `Date`,
    {_weekday_expr("yd.`Date`")} AS `День недели`,
    yd.week_start AS week_start,
    yd.`CampaignId` AS `CampaignId`,
    yd.`CampaignName` AS `CampaignName`,
    yd.`AdGroupId` AS `AdGroupId`,
    yd.`AdGroupName` AS `AdGroupName`,
    yd.`AdNetworkType` AS `AdNetworkType`,
    yd.`Device` AS `Device`,
    toDecimal64(yd.`Impressions`, 6) AS `Impressions`,
    toDecimal64(yd.`Clicks`, 6) AS `Clicks`,
    toDecimal64(yd.total_cost, 6) AS total_cost,
    coalesce(nullIf(la.domain, ''), {_gs_pick_expr("domain")}) AS domain,
    yd.`RlAdjustmentId` AS `RlAdjustmentId`,
    toString(yd.`RlAdjustmentId`) AS `RlAdjustmentId_total`,
    yd.campaign_code AS campaign_code,
    yd.tp AS tp,
    yd.cpc_cpa AS cpc_cpa,
    yd.site_quiz AS site_quiz,
    yd.adgroup_code AS adgroup_code,
    yd.account_login AS account_login,
    coalesce(nullIf(yd.manager_login, ''), {_gs_pick_expr("directologist")}) AS manager_login,
    {_ag_parts_expr("yd.")},
    '' AS `марки авто`,
    ifNull(nullIf(coalesce(la.crm_name, crm.crm_name), ''), 'Не указана') AS `Название crm`,
    if(la.kol_vo_zayavok > 0, if(la.zvonki_cdr, 'Звонки_CDR', 'Заявка'), NULL) AS `тип_заявки`,
    ifNull(la.kol_vo_zayavok, toDecimal64(0, 6)) AS kol_vo_zayavok,
    ifNull(la.korr, toDecimal64(0, 6)) AS korr,
    ifNull(la.kval, toDecimal64(0, 6)) AS kval,
    ifNull(la.priezd, toDecimal64(0, 6)) AS priezd,
    ifNull(la.prodazhi, toDecimal64(0, 6)) AS prodazhi,
    ifNull(la.nekorr, toDecimal64(0, 6)) AS nekorr,
    ifNull(la.ne_otvechaet, toDecimal64(0, 6)) AS ne_otvechaet,
    ifNull(la.filtr, toDecimal64(0, 6)) AS filtr,
    ifNull(la.nedozvon, toDecimal64(0, 6)) AS nedozvon,
    ifNull(la.priedet, toDecimal64(0, 6)) AS priedet,
    ifNull(la.dohod_do_kredita, 0) AS dohod_do_kredita,
    ifNull(la.dobro, 0) AS dobro,
    {_gs_pick_expr("status")} AS `статус`,
    {_direct_specialist_expr()} AS `специалист`,
    {_gs_pick_expr("site_type")} AS `тип_сайта`,
    {_gs_pick_expr("template")} AS `шаблон`,
    {_gs_pick_expr("salon")} AS `салон`,
    {_gs_pick_expr("city")} AS `город`,
    {_gs_pick_expr("region")} AS `регион`,
    {_gs_pick_expr("direction")} AS direction,
    if(ifNull(yd.campaign_code, '') = '', 'неверный кодер', NULL) AS `неверный_кодер_new`,
    la.fid AS fid,
    {_gs_pick_expr("project_manager")} AS `проджект`,
    {_gs_pick_expr("client_id")} AS `id_салона`,
    {_gs_pick_expr("sales_manager")} AS `менеджер`,
    {_direct_source_expr("yd.")} AS `источник`,
    {_direct_napravlenie_expr("yd.")} AS `направление`,
    concat(toString(yd.`CampaignId`), '|', ifNull(yd.`CampaignName`, '')) AS `номер кампании | название кампании`,
    concat(toString(yd.`AdGroupId`), '|', ifNull(yd.`AdGroupName`, '')) AS `номер группы | название группы`,
    CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
    CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
    concat(ifNull(yd.account_login, ''), '|', ifNull(coalesce(nullIf(la.domain, ''), {_gs_pick_expr("domain")}), '')) AS `аккаунт|сайт`,
    CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
    CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
    'Яндекс' AS `поставщик`,
    {_direct_source_table_expr("yd.")} AS _source_table,
    CAST(NULL, 'Nullable(String)') AS cascade_level,
    cs.campaign_status AS campaign_status,
    cs.payment_model AS payment_model
FROM yd
LEFT JOIN la ON la.key3 = yd.key3
LEFT JOIN gs_best gs ON gs.match_login_key = yd.account_login AND gs.match_date = yd.`Date`
LEFT JOIN gs_domain gs_dir ON gs_dir.domain_key = la.domain_key
LEFT JOIN crm_by_domain crm ON crm.domain_key = lower(trim(ifNull(coalesce(nullIf(la.domain, ''), {_gs_pick_expr("domain")}), '')))
LEFT JOIN ad_analytics.campaign_status_v cs ON cs.`CampaignId` = yd.`CampaignId`
WHERE ifNull({_gs_pick_expr("direction")}, 'Авто') = 'Авто'
"""


def _lead_source_columns(
    source_expr: str,
    direction_expr: str,
    provider: str,
    source_table_expr: str,
) -> list[tuple[str, str]]:
    """Единый УПОРЯДОЧЕННЫЙ список колонок всех лид-веток: (алиас, SQL-выражение).

    Порядок обязан совпадать с `_build_direct_sql` — все ветки льются одним
    `INSERT INTO <shadow> SELECT …`, а он в ClickHouse ПОЗИЦИОННЫЙ: перестановка
    колонок молча разложит значения не по тем полям. Поэтому список ровно один на
    все лид-ветки, а расхождения задаются через `overrides` в
    `_build_lead_source_sql`, а не копией SELECT'а.
    """
    return [
        ("key3", "l.key3"),
        ("Date", "l.created_date"),
        ("День недели", _weekday_expr("l.created_date")),
        ("week_start", "toStartOfWeek(l.created_date, 1)"),
        ("CampaignId", "l.campaign_id"),
        ("CampaignName", "CAST(NULL, 'Nullable(String)')"),
        ("AdGroupId", "l.group_id"),
        ("AdGroupName", "CAST(NULL, 'Nullable(String)')"),
        ("AdNetworkType", "CAST(NULL, 'Nullable(String)')"),
        ("Device", "CAST(NULL, 'Nullable(String)')"),
        ("Impressions", "toDecimal64(0, 6)"),
        ("Clicks", "toDecimal64(0, 6)"),
        ("total_cost", "toDecimal64(0, 6)"),
        ("domain", "l.domain"),
        ("RlAdjustmentId", "l.correction_id"),
        ("RlAdjustmentId_total", "toString(l.correction_id)"),
        ("campaign_code", "CAST(NULL, 'Nullable(String)')"),
        ("tp", "CAST(NULL, 'Nullable(String)')"),
        ("cpc_cpa", "CAST(NULL, 'Nullable(String)')"),
        ("site_quiz", "CAST(NULL, 'Nullable(String)')"),
        ("adgroup_code", "CAST(NULL, 'Nullable(String)')"),
        ("account_login", "gs.login_key"),
        ("manager_login", "gs.directologist"),
        ("ag_part1", "CAST(NULL, 'Nullable(String)')"),
        ("ag_part2", "CAST(NULL, 'Nullable(String)')"),
        ("ag_part3", "CAST(NULL, 'Nullable(String)')"),
        ("ag_part4", "CAST(NULL, 'Nullable(String)')"),
        ("ag_part5", "CAST(NULL, 'Nullable(String)')"),
        ("ag_part6", "CAST(NULL, 'Nullable(String)')"),
        ("ag_part7", "CAST(NULL, 'Nullable(String)')"),
        ("марки авто", "''"),
        ("Название crm", _crm_name_expr("l.source_type")),
        ("тип_заявки", _claim_type_expr("l.utm_content", "l.deal_type")),
        ("kol_vo_zayavok", "l.kol_vo_zayavok"),
        ("korr", "l.korr"),
        ("kval", "l.kval"),
        ("priezd", "l.priezd"),
        ("prodazhi", "l.prodazhi"),
        ("nekorr", "l.nekorr"),
        ("ne_otvechaet", "l.ne_otvechaet"),
        ("filtr", "l.filtr"),
        ("nedozvon", "l.nedozvon"),
        ("priedet", "l.priedet"),
        ("dohod_do_kredita", "l.dohod_do_kredita"),
        ("dobro", "l.dobro"),
        ("статус", "gs.status"),
        ("специалист", _domain_specialist_expr("gs")),
        ("тип_сайта", "gs.site_type"),
        ("шаблон", "gs.template"),
        ("салон", "coalesce(nullIf(l.salon, ''), gs.salon)"),
        ("город", "gs.city"),
        ("регион", "gs.region"),
        ("direction", "gs.direction"),
        ("неверный_кодер_new", "CAST(NULL, 'Nullable(String)')"),
        ("fid", "l.fid"),
        ("проджект", "gs.project_manager"),
        ("id_салона", "gs.client_id"),
        ("менеджер", "gs.sales_manager"),
        ("источник", source_expr),
        ("направление", direction_expr),
        ("номер кампании | название кампании", "CAST(NULL, 'Nullable(String)')"),
        ("номер группы | название группы", "CAST(NULL, 'Nullable(String)')"),
        ("План заявки", "CAST(NULL, 'Nullable(Int32)')"),
        ("План приезда", "CAST(NULL, 'Nullable(Int32)')"),
        ("аккаунт|сайт", "concat(ifNull(gs.login_key, ''), '|', ifNull(l.domain, ''))"),
        ("priezd_arrival_date", "CAST(NULL, 'Nullable(Int64)')"),
        ("prodazhi_arrival_date", "CAST(NULL, 'Nullable(Int64)')"),
        ("поставщик", f"'{provider}'"),
        ("_source_table", source_table_expr),
        ("cascade_level", "CAST(NULL, 'Nullable(String)')"),
        ("campaign_status", "CAST(NULL, 'Nullable(String)')"),
        ("payment_model", "CAST(NULL, 'Nullable(String)')"),
    ]


def _build_lead_source_sql(
    table: str,
    source_filter: str,
    source_name: str,
    direction_name: str,
    provider: str,
    lead_date_filter: str = "",
    source_table: str | None = None,
    extra_where: str = "",
    extra_ctes: str = "",
    extra_joins: str = "",
    overrides: dict[str, str] | None = None,
) -> str:
    columns = _lead_source_columns(
        f"'{source_name}'",
        f"'{direction_name}'",
        provider,
        f"'{source_table or table.replace('big_analytics_', '')}'",
    )
    if overrides:
        known = {alias for alias, _ in columns}
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise ValueError(f"unknown lead-source column overrides: {unknown}")
        columns = [(alias, overrides.get(alias, expr)) for alias, expr in columns]
    select_list = ",\n    ".join(f"{expr} AS {q(alias)}" for alias, expr in columns)
    return f"""
CREATE TABLE ad_analytics.{table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(key3, ''))
AS
WITH
{_gs_account_cte()},
{extra_ctes}
lead_scored AS
(
    SELECT
        l.*,
        lower(trim(ifNull(l.domain, ''))) AS domain_key,
        {_metric_expr("l.status", "l.reason", "l.source_type", "l.salon")}
    FROM ad_analytics.{LEADS_DEDUPED_STAGE} l
    WHERE {source_filter}
      {lead_date_filter}
),
gs_domain_best AS
(
    SELECT *
    FROM
    (
        SELECT
            ld.domain_key AS domain_key,
            ld.date_val AS match_date,
            gs.domain,
            gs.status,
            gs.directologist,
            gs.site_type,
            gs.template,
            gs.salon,
            gs.city,
            gs.region,
            gs.direction,
            gs.direction_main,
            gs.project_manager,
            gs.client_id,
            gs.sales_manager,
            gs.login_key,
            multiIf(
                ifNull(gs.domain, '') = '', 99,
                ifNull(trim(gs.launch_date), '') = '' AND ifNull(trim(gs.block_date), '') = '', 2,
                (ifNull(trim(gs.launch_date), '') = '' OR ld.date_val >= toDate(parseDateTimeBestEffortOrNull(gs.launch_date)))
                    AND (ifNull(trim(gs.block_date), '') = '' OR ld.date_val < toDate(parseDateTimeBestEffortOrNull(gs.block_date))),
                1,
                3
            ) AS match_priority,
            row_number() OVER (
                PARTITION BY ld.domain_key, ld.date_val
                ORDER BY
                    match_priority ASC,
                    ifNull(toDate(parseDateTimeBestEffortOrNull(gs.launch_date)), toDate('1900-01-01')) DESC,
                    ifNull(gs.domain, '') ASC
            ) AS rn
        FROM
        (
            SELECT DISTINCT domain_key, created_date AS date_val
            FROM lead_scored
            WHERE domain_key != ''
        ) ld
        LEFT JOIN reference_data.gsheet_sites gs
          ON lower(trim(ifNull(gs.domain, ''))) = ld.domain_key
    )
    WHERE rn = 1
)
SELECT
    {select_list}
FROM lead_scored l
LEFT JOIN gs_domain_best gs ON gs.domain_key = l.domain_key AND gs.match_date = l.created_date
LEFT JOIN crm_by_domain crm ON crm.domain_key = l.domain_key
{extra_joins}
WHERE ifNull(l.created_date, toDate('1970-01-01')) >= toDate('2026-01-01')
  {extra_where}
"""


def _build_empty_like_sql(table: str) -> str:
    return f"""
CREATE TABLE ad_analytics.{table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(`Date`)
ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(key3, ''))
AS
SELECT *
FROM ad_analytics.{SOURCE_STORE}
WHERE 0
"""


def _crop_source_expr(prefix: str = "l.") -> str:
    return f"""
        multiIf(
            lowerUTF8(ifNull({prefix}utm_source, '')) = 'vkads', 'VK Ads',
            lowerUTF8(ifNull({prefix}utm_source, '')) = 'max', 'Посевы_Max',
            lowerUTF8(ifNull({prefix}utm_source, '')) IN ('vk', 'vk_groups', 'vk_storis'), 'Посевы_VK',
            match(lowerUTF8(ifNull({prefix}utm_campaign, '')), '(^|_)vk($|_|[0-9])'), 'Посевы_VK',
            match(lowerUTF8(ifNull({prefix}utm_campaign, '')), '(^|_)max($|_|[0-9])'), 'Посевы_Max',
            lowerUTF8(ifNull({prefix}utm_source, '')) IN ('telegram', 'stories_tg', 'telegram_storis', 'instagram'), 'Посевы_Telegram',
            ifNull({prefix}utm_source, '') = '', 'Посевы_Telegram',
            'Посевы_Telegram'
        )
    """


def _crop_source_table_expr(prefix: str = "l.") -> str:
    return f"""
        multiIf(
            ifNull({prefix}utm_source, '') IN ('telegram', 'stories_tg'), 'telegram',
            ifNull({prefix}utm_source, '') IN ('max', 'vk', 'vk_groups', 'vk_storis', 'telegram_storis'), 'social_посевы',
            ifNull({prefix}utm_source, '') = 'vkads' AND match(ifNull({prefix}utm_campaign, ''), '^[0-9]+$'), 'vk_ads',
            ifNull({prefix}utm_source, '') = 'vkads', 'vk_zero',
            'crop_targeting'
        )
    """


def _crop_provider_expr(prefix: str = "l.") -> str:
    return f"if(ifNull({prefix}utm_source, '') = 'vkads', 'ВК Реклама', 'Посевы')"


def _crop_overrides(prefix: str = "l.") -> dict[str, str]:
    return {
        "источник": _crop_source_expr(prefix),
        "_source_table": _crop_source_table_expr(prefix),
        "поставщик": _crop_provider_expr(prefix),
    }


_CROP_ACCOUNT_DOMAIN_SUBQUERY = """
    SELECT DISTINCT lowerUTF8(trim(ifNull(ca.`Сайт`, '')))
    FROM ad_analytics.gsheets_crop_targeting_account ca
    WHERE ifNull(ca.`Сайт`, '') != ''
"""


def _build_crop_sql() -> str:
    return _build_lead_source_sql(
        "big_analytics_crop_targeting",
        _crop_source_filter(),
        "Посевы",
        "Комплекс",
        "Посевы",
        overrides=_crop_overrides(),
    )


def _build_seo_sql(lead_date_filter: str = "") -> str:
    filt = """
(
    ifNull(utm_source, '') = ''
    OR (utm_source = 'seo' AND ifNull(utm_medium, '') = 'organic')
)
AND lowerUTF8(trim(ifNull(domain, ''))) IN (
    SELECT lowerUTF8(trim(ifNull(gs2.domain, '')))
    FROM reference_data.gsheet_sites gs2
    WHERE ifNull(gs2.domain, '') != ''
)
"""
    return _build_lead_source_sql(
        "big_analytics_seo",
        filt,
        "SEO",
        "Комплекс",
        "Victory",
        lead_date_filter,
        overrides={
            "источник": (
                "multiIf("
                f"l.domain_key IN ({_CROP_ACCOUNT_DOMAIN_SUBQUERY}), 'Посевы_SEO', "
                "ifNull(gs.direction_main, '') = 'Посевы', 'Посевы_SEO', "
                "gs.status = 'SEO Flow', 'SEO Flow', "
                "'SEO')"
            ),
            "поставщик": (
                f"if(l.domain_key IN ({_CROP_ACCOUNT_DOMAIN_SUBQUERY}) "
                "OR ifNull(gs.direction_main, '') = 'Посевы', 'Посевы', 'Victory')"
            ),
        },
    )


def _build_pixel_sql(lead_date_filter: str = "") -> str:
    raise RuntimeError("big_analytics_pixel is built by step5_build_pixel, not by step3")


# ══════════════════════════════════════════════════════════════════════════════
# DIRECT_LEAD_BRANCHES_2026-08-05 — две потерянные ветки лидов Директа
# ------------------------------------------------------------------------------
# Основная ветка (`_build_direct_sql`) идёт ОТ ТАБЛИЦЫ РАСХОДА: `FROM yd LEFT JOIN la`.
# Лид, чьего key3 нет в статистике Директа, не порождает ни одной строки — исчезает
# из витрины целиком. В v5 такие лиды жили отдельными ветками (step3.py:467/628/1109
# → direct_unmatched, :475/1287 → direct_zero) с total_cost=NULL.
#
# Обе ветки идут ОТ ЛИДА (`_build_lead_source_sql`), поэтому:
#   * total_cost / Impressions / Clicks = 0 — расход НЕ дублируется, только воронка;
#   * `_source_table` = 'direct_unmatched' / 'direct_zero' — ровно те значения, что
#     ждёт GOLDEN_SOURCES в data_check/verify_big_analytics.py:127.
#
# Дизъюнктность трёх веток (лид попадает ровно в одну):
#   direct           — key3 ∈ raw_yandex за тот же день;
#   direct_unmatched — key3 ∉ raw_yandex И key3 не '…|0|0|0|0';
#   direct_zero      — key3 вида '…|0|0|0|0' (в raw_yandex таких ключей 0 строк).
# Anti-join ограничен окном батча [lo, hi) СПЕЦИАЛЬНО: key3 начинается с даты
# (created_date у лида, `Date` у расхода), поэтому лид дня D может совпасть только
# со строкой расхода дня D. Ограничение окна = точный эквивалент глобального
# anti-join, но без построения множества из 4.7 млн ключей на каждый батч.
#
# `gs.direction = 'Авто'` — СТРОГОЕ равенство (NULL исключается) — воспроизводит
# v5-гейт `FROM big_analytics_direct WHERE direction = 'Авто'`
# (v5 step6_build_full/step6.py:114): лид на домене, которого нет в gsheet_sites,
# в витрину v5 не попадал. Без этого гейта ветки притащили бы ~273 тыс. лидов
# доменов-«ничьих» (domain_id IS NULL в CRM). ⚠️ Именно ЗДЕСЬ строгое равенство,
# а не `ifNull(…, 'Авто')` как в основной ветке: там гейт стоит поверх строки
# расхода, у которой домен известен из аккаунта Директа, здесь — поверх лида.
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# CASCADE_MATCH_2026-07-03 (порт v5 step3.py:542-631, 1220-1235) — каскадный матчинг
# ------------------------------------------------------------------------------
# Лид, чьего key3 нет в статистике Директа один-в-один, всё-таки может принадлежать
# известной кампании: CRM не парсит `r:` (correction_id), device в UTM расходится с
# device Директа, а у tp6/tp7 group_id вообще занулён. v5 добирает такие лиды
# каскадом по УКОРОЧЕННОМУ ключу и переводит их из `direct_unmatched` в `direct`:
#   level 4 — date|campaign|group|device      (отброшен correction_id)
#   level 3 — date|campaign|group             (отброшены device + correction_id)
#   level 2 — date|campaign                   (отброшен ещё и group_id)
# Каждый лид матчится РОВНО НА ОДНОМ уровне (v5: level3 исключает пойманных level4
# и т.д.), при нескольких кандидатах берётся строка с max(total_cost).
#
# Порт использует ОДИН join вместо трёх: k4-равенство ⟹ k3a-равенство ⟹ k2-равенство,
# поэтому join по k2 даёт полное множество кандидатов всех уровней, а
# `ORDER BY cascade_rank DESC, match_cost DESC` внутри лида воспроизводит ту же
# выборку, что три последовательных уровня v5 (сначала самый глубокий уровень,
# внутри уровня — самый дорогой кандидат).
#
# Расход НЕ дублируется: total_cost/Impressions/Clicks = 0, строка расхода уже
# посчитана основной веткой. Меняется только атрибуция воронки лида.
#
# Дизъюнктность с `direct_unmatched` — по построению, а не по вычитанию множеств:
# лид попадает в каскад ⟺ у его key3 есть k2-кандидат в статистике; ветка
# unmatched берёт ровно отрицание этого предиката (`_cascade_has_match_filter`).
# ══════════════════════════════════════════════════════════════════════════════


def _key3_prefix(expr: str, parts: int) -> str:
    """Первые `parts` компонент key3 (`date|campaign|group|device|correction`)."""
    return f"arrayStringConcat(arraySlice(splitByChar('|', ifNull({expr}, '')), 1, {parts}), '|')"


def _yd_key3_window(lo: str, hi: str) -> str:
    return (
        "SELECT key3 FROM ad_analytics.raw_yandex "
        f"WHERE `Date` >= toDate('{lo}') AND `Date` < toDate('{hi}') AND ifNull(key3, '') != ''"
    )


def _direct_lead_no_spend_filter(lo: str, hi: str, prefix: str = "l.") -> str:
    """Универс лидов Директа без прямой пары в статистике: каскад + unmatched.

    Одно определение на обе ветки — расхождение фильтров молча потеряло бы лиды
    (лид не попал бы ни в одну ветку) или задвоило бы их.
    """
    return f"""
{_direct_lead_universe_filter(prefix)}
      AND {prefix}key3 NOT LIKE '{_ZERO_KEY3_PATTERN}'
      AND {prefix}key3 NOT IN ({_yd_key3_window(lo, hi)})
"""


def _cascade_has_match_filter(lo: str, hi: str, negate: bool, prefix: str = "l.") -> str:
    """Предикат «у лида есть кандидат каскада» (k2 = date|campaign есть в статистике)."""
    op = "NOT IN" if negate else "IN"
    return f"""AND {_key3_prefix(prefix + "key3", 2)} {op} (
          SELECT {_key3_prefix("key3", 2)} FROM ({_yd_key3_window(lo, hi)})
      )"""


def _cascade_ctes(lo: str, hi: str) -> str:
    raw_date_filter = f"AND ry.`Date` >= toDate('{lo}') AND ry.`Date` < toDate('{hi}')"
    yd_fields = [
        "`CampaignId`",
        "`CampaignName`",
        "`AdGroupId`",
        "`AdGroupName`",
        "`AdNetworkType`",
        "`Device`",
        "`RlAdjustmentId`",
        "account_login",
        "manager_login",
        "campaign_code",
        "tp",
        "cpc_cpa",
        "site_quiz",
        "adgroup_code",
    ]
    yk_list = ",\n        ".join(f"yk.{field} AS {field}" for field in yd_fields)
    best_list = ",\n        ".join(yd_fields)
    return f"""
{_yd_agg_cte(raw_date_filter).strip()},
yd_cascade AS
(
    SELECT
        {", ".join(yd_fields)},
        total_cost,
        {_key3_prefix("key3", 4)} AS k4,
        {_key3_prefix("key3", 3)} AS k3a,
        {_key3_prefix("key3", 2)} AS k2
    FROM yd
),
lead_cascade AS
(
    SELECT
        l.key3 AS key3,
        {_key3_prefix("l.key3", 4)} AS k4,
        {_key3_prefix("l.key3", 3)} AS k3a,
        {_key3_prefix("l.key3", 2)} AS k2
    FROM ad_analytics.{LEADS_DEDUPED_STAGE} l
    WHERE {_direct_lead_no_spend_filter(lo, hi)}
      {_lead_date_filter(lo, hi)}
),
cascade_ranked AS
(
    SELECT
        lk.key3 AS lead_key3,
        multiIf(lk.k4 = yk.k4, 4, lk.k3a = yk.k3a, 3, 2) AS cascade_rank,
        yk.total_cost AS match_cost,
        {yk_list}
    FROM yd_cascade yk
    INNER JOIN lead_cascade lk ON lk.k2 = yk.k2
),
cascade_best AS
(
    SELECT
        lead_key3,
        toString(cascade_rank) AS cascade_level,
        {best_list}
    FROM
    (
        SELECT
            *,
            row_number() OVER (PARTITION BY lead_key3 ORDER BY cascade_rank DESC, match_cost DESC) AS rn
        FROM cascade_ranked
    )
    WHERE rn = 1
),
"""


def _build_direct_cascade_sql(lo: str, hi: str) -> str:
    """Лиды Директа, добранные каскадом к кампании расхода (v5: `_source_table='direct'`)."""
    filt = _direct_lead_no_spend_filter(lo, hi) + "      " + _cascade_has_match_filter(lo, hi, negate=False)
    account_login = "coalesce(nullIf(ca.account_login, ''), gs.login_key)"
    overrides = {
        "CampaignId": "ca.`CampaignId`",
        "CampaignName": "ca.`CampaignName`",
        "AdGroupId": "ca.`AdGroupId`",
        "AdGroupName": "ca.`AdGroupName`",
        "AdNetworkType": "ca.`AdNetworkType`",
        "Device": "ca.`Device`",
        "RlAdjustmentId": "ca.`RlAdjustmentId`",
        "RlAdjustmentId_total": "toString(ca.`RlAdjustmentId`)",
        "campaign_code": "ca.campaign_code",
        "tp": "ca.tp",
        "cpc_cpa": "ca.cpc_cpa",
        "site_quiz": "ca.site_quiz",
        "adgroup_code": "ca.adgroup_code",
        "account_login": account_login,
        "manager_login": "coalesce(nullIf(ca.manager_login, ''), gs.directologist)",
        "неверный_кодер_new": "if(ifNull(ca.campaign_code, '') = '', 'неверный кодер', NULL)",
        "источник": _direct_source_expr("ca."),
        "направление": _direct_napravlenie_expr("ca."),
        "номер кампании | название кампании": "concat(toString(ca.`CampaignId`), '|', ifNull(ca.`CampaignName`, ''))",
        "номер группы | название группы": "concat(toString(ca.`AdGroupId`), '|', ifNull(ca.`AdGroupName`, ''))",
        "аккаунт|сайт": f"concat(ifNull({account_login}, ''), '|', ifNull(l.domain, ''))",
        "_source_table": _direct_source_table_expr("ca."),
        "cascade_level": "ca.cascade_level",
        "campaign_status": "cs.campaign_status",
        "payment_model": "cs.payment_model",
        **{f"ag_part{idx}": expr for idx, expr in enumerate(_ag_part_exprs("ca."), start=1)},
    }
    return _build_lead_source_sql(
        "big_analytics_direct_cascade",
        filt,
        "Контекст",
        "Контекст",
        "Яндекс",
        _lead_date_filter(lo, hi),
        source_table="direct",
        extra_where="AND gs.direction = 'Авто'",
        extra_ctes=_cascade_ctes(lo, hi).strip() + "\n",
        extra_joins=(
            "INNER JOIN cascade_best ca ON ca.lead_key3 = l.key3\n"
            "LEFT JOIN ad_analytics.campaign_status_v cs ON cs.`CampaignId` = ca.`CampaignId`"
        ),
        overrides=overrides,
    )


def _build_direct_unmatched_sql(lo: str, hi: str) -> str:
    """Лиды Директа, не добранные даже каскадом (v5: direct_unmatched)."""
    filt = _direct_lead_no_spend_filter(lo, hi) + "      " + _cascade_has_match_filter(lo, hi, negate=True)
    return _build_lead_source_sql(
        "big_analytics_direct_unmatched",
        filt,
        "Контекст",
        "Комплекс",
        "Яндекс",
        _lead_date_filter(lo, hi),
        source_table="direct_unmatched",
        extra_where="AND gs.direction = 'Авто'",
        extra_ctes=_posev_repaint_cte() + ",\n",
        overrides={"источник": _unmatched_source_expr()},
    )


def _build_direct_zero_sql(lo: str, hi: str) -> str:
    """Лиды Директа без campaign_id (key3 '…|0|0|0|0') — v5: direct_zero."""
    filt = f"""
{_direct_lead_universe_filter("l.")}
      AND l.key3 LIKE '{_ZERO_KEY3_PATTERN}'
"""
    return _build_lead_source_sql(
        "big_analytics_direct_zero",
        filt,
        "Контекст",
        "Комплекс",
        "Яндекс",
        _lead_date_filter(lo, hi),
        source_table="direct_zero",
        extra_where="AND gs.direction = 'Авто'",
        extra_ctes=_posev_repaint_cte() + ",\n",
        overrides={"источник": f"if({_POSEV_REPAINT_PREDICATE}, 'Посевы_Telegram', 'Контекст')"},
    )


def _build_crop_sql_batched(lead_date_filter: str = "") -> str:
    return _build_lead_source_sql(
        "big_analytics_crop_targeting",
        _crop_source_filter(),
        "Посевы",
        "Комплекс",
        "Посевы",
        lead_date_filter,
        overrides=_crop_overrides(),
    )


def _select_from_create(create_sql: str) -> str:
    marker = "\nAS\n"
    if marker not in create_sql:
        raise ValueError("CREATE TABLE SQL does not contain expected AS marker")
    return create_sql.split(marker, 1)[1].strip()


def _swap_shadow(client, table: str) -> None:
    swap_shadow(client, f"ad_analytics.{table}", f"ad_analytics.{table}_new")


def _rebuild_batched(client, table: str, empty_create_sql: str, batch_select_sqls: list[str]):
    target = f"ad_analytics.{table}"
    shadow = f"ad_analytics.{table}_new"
    client = _command_with_retry(client, f"DROP TABLE IF EXISTS {shadow} SYNC", label=f"{table} shadow drop")
    client = _command_with_retry(
        client,
        empty_create_sql.replace(target, shadow, 1),
        label=f"{table} shadow create",
        settings=STEP3_QUERY_SETTINGS,
    )
    for idx, select_sql in enumerate(batch_select_sqls, start=1):
        t0 = time.perf_counter()
        client = _command_with_retry(
            client,
            f"INSERT INTO {shadow}\n{select_sql}",
            label=f"{table} batch {idx}/{len(batch_select_sqls)}",
            settings=STEP3_QUERY_SETTINGS,
        )
        logger.info("    batch %s %d/%d inserted за %.1f сек", table, idx, len(batch_select_sqls), time.perf_counter() - t0)
    _swap_shadow(client, table)
    return client, int(client.query(f"SELECT count() FROM {target}", settings=STEP3_QUERY_SETTINGS).result_rows[0][0])


def _source_types_sql(source_types: tuple[str, ...]) -> str:
    return ", ".join(f"'{item}'" for item in source_types)


def recreate_source_views(client) -> None:
    direct_types = _source_types_sql(DIRECT_SOURCE_TYPES)
    crop_types = _source_types_sql(CROP_SOURCE_TYPES)
    replace_view(
        client,
        "ad_analytics.big_analytics_direct",
        f"SELECT * FROM ad_analytics.{SOURCE_STORE} WHERE _source_table IN ({direct_types})",
    )
    replace_view(
        client,
        "ad_analytics.big_analytics_seo",
        f"SELECT * FROM ad_analytics.{SOURCE_STORE} WHERE _source_table = 'seo'",
    )
    replace_view(
        client,
        "ad_analytics.big_analytics_pixel",
        f"SELECT * FROM ad_analytics.{SOURCE_STORE} WHERE _source_table = 'pixel'",
    )
    replace_view(
        client,
        "ad_analytics.big_analytics_crop_targeting",
        f"SELECT * FROM ad_analytics.{SOURCE_STORE} WHERE _source_table IN ({crop_types})",
    )
    replace_view(
        client,
        "ad_analytics.big_analytics_reviews",
        f"SELECT * FROM ad_analytics.{SOURCE_STORE} WHERE _source_table IN ('reviews', 'direct_account_reviews')",
    )


def _reviews_domain_expr(login_expr: str, site_expr: str) -> str:
    """Site if known, else a stable synthetic per-login domain — byte-identical rule to the
    retired PostgreSQL bridge (`'reviews-' || REPLACE(...)`), just CH `concat`/`replaceAll`."""
    synthetic = f"concat('reviews-', replaceAll(lower(trim(ifNull({login_expr}, 'unknown'))), '_', '-'), '.local')"
    return f"coalesce(nullIf({site_expr}, ''), {synthetic})"


def _build_reviews_sql(target_table: str) -> str:
    """CH-native replacement for the retired PostgreSQL bridge
    (`yandex_direct_raw.*` on Victory PG). Source is now
    `ad_analytics.yandex_direct_reports_reviews` / `yandex_direct_account_reviews`,
    both filled weekly by `step_cron_night/direct_account_reviews/run.py`.

    REVIEWS_DEDUPE_FANOUT_FIX_2026-08-24: `аккаунт` in yandex_direct_account_reviews is not
    unique, and the old PG bridge's plain `LEFT JOIN ... ON d.аккаунт = r.login` duplicated
    every stat row for a repeated account (e.g. porg-rw7i2sgf, 86 rows / 10810.62 RUB, id
    26/258 — a genuine Sheets data-entry duplicate, both rows carry the identical `сайт`).
    `argMax(col, id)` below deterministically picks one row per account, collapsing any
    fan-out to 1:1 — safe for a genuine duplicate.

    KNOWN LIMITATION — porg-j47mlyp5 (id 259/274) is a *different* case argMax cannot
    represent correctly: its two rows carry a DIFFERENT `сайт` (car-review.site vs
    avto-world-obzor.site). `yandex_direct_reports_reviews` shows this login's stats fall
    into two disjoint periods (2026-06-04..06-19, then 2026-08-12..08-16) — but both periods
    are the SAME campaign (`CampaignId 710372643`, "Заказ на Автостайл №56478834",
    `uniqExact(CampaignId)=1` across the whole login), one campaign paused 2026-06-20..08-11
    and resumed, not two different campaigns as an earlier version of this comment claimed.
    That only confirms the two `сайт` values are genuinely tied to different time windows on
    the SAME account, not to different campaigns — the underlying problem stands: `сайт`
    changed over time for this account. `yandex_direct_account_reviews` has NO date/validity
    column (only id,
    город, салон, аккаунт, сайт, агентский аккаунт), so a time-varying site cannot be
    expressed here at all; `argMax(col, id)` just picks whichever row has the highest `id`
    (incidental sheet-row order, not a designed "latest wins" rule) and silently
    mis-attributes `domain`/`аккаунт|сайт` for the other period's rows. `parse_records`
    (load_reviews.py) logs a warning for this case so it stays visible. Fixing it for real
    needs a validity-period column in the reference sheet — a business decision, not made
    here; do not change the join to "fix" this without that decision.
    """
    domain_expr = _reviews_domain_expr("r.login", "a.`сайт`")
    return f"""
CREATE TABLE ad_analytics.{target_table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(`Date`)
ORDER BY (`Date`, ifNull(`CampaignId`, 0), ifNull(key3, ''))
AS
WITH
accounts AS
(
    SELECT
        `аккаунт` AS account,
        argMax(`город`, id) AS `город`,
        argMax(`салон`, id) AS `салон`,
        argMax(`сайт`, id) AS `сайт`
    FROM ad_analytics.yandex_direct_account_reviews
    GROUP BY `аккаунт`
),
gs AS
(
    SELECT
        lower(trim(salon)) AS salon_key,
        argMax(project_manager, _loaded_at) AS project_manager,
        argMax(client_id, _loaded_at) AS client_id,
        argMax(sales_manager, _loaded_at) AS sales_manager
    FROM reference_data.gsheet_sites
    WHERE ifNull(trim(salon), '') != ''
    GROUP BY lower(trim(salon))
)
SELECT
    '' AS key3,
    r.`Date` AS `Date`,
    {_weekday_expr("r.`Date`")} AS `День недели`,
    toStartOfWeek(r.`Date`, 1) AS week_start,
    ifNull(r.`CampaignId`, 0) AS `CampaignId`,
    r.`CampaignName` AS `CampaignName`,
    ifNull(r.`AdGroupId`, 0) AS `AdGroupId`,
    r.`AdGroupName` AS `AdGroupName`,
    if(r.`AdNetworkType` = 'SEARCH', 'Отзывы', r.`AdNetworkType`) AS `AdNetworkType`,
    r.`Device` AS `Device`,
    toDecimal64(ifNull(r.`Impressions`, 0), 6) AS `Impressions`,
    toDecimal64(ifNull(r.`Clicks`, 0), 6) AS `Clicks`,
    toDecimal64(ifNull(r.`Cost`, 0), 6) AS total_cost,
    {domain_expr} AS domain,
    ifNull(r.`RlAdjustmentId`, 0) AS `RlAdjustmentId`,
    'отзывы' AS `RlAdjustmentId_total`,
    CAST(NULL, 'Nullable(String)') AS campaign_code,
    '' AS tp,
    '' AS cpc_cpa,
    '' AS site_quiz,
    CAST(NULL, 'Nullable(String)') AS adgroup_code,
    ifNull(r.login, '') AS account_login,
    'отзывы' AS manager_login,
    '' AS ag_part1, '' AS ag_part2, '' AS ag_part3, '' AS ag_part4,
    '' AS ag_part5, '' AS ag_part6, '' AS ag_part7,
    '' AS `марки авто`,
    'отзывы' AS `Название crm`,
    'Отзывы' AS `тип_заявки`,
    toDecimal64(0, 6) AS kol_vo_zayavok,
    toDecimal64(0, 6) AS korr,
    toDecimal64(0, 6) AS kval,
    toDecimal64(0, 6) AS priezd,
    toDecimal64(0, 6) AS prodazhi,
    toDecimal64(0, 6) AS nekorr,
    toDecimal64(0, 6) AS ne_otvechaet,
    toDecimal64(0, 6) AS filtr,
    toDecimal64(0, 6) AS nedozvon,
    toDecimal64(0, 6) AS priedet,
    0 AS dohod_do_kredita,
    0 AS dobro,
    CAST(NULL, 'Nullable(String)') AS `статус`,
    'Караваев Михаил' AS `специалист`,
    'отзывы' AS `тип_сайта`,
    'отзывы' AS `шаблон`,
    a.`салон` AS `салон`,
    a.`город` AS `город`,
    CAST(NULL, 'Nullable(String)') AS `регион`,
    'Авто' AS direction,
    CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
    CAST(NULL, 'Nullable(String)') AS fid,
    nullIf(trim(gs.project_manager), '') AS `проджект`,
    nullIf(trim(gs.client_id), '') AS `id_салона`,
    nullIf(trim(gs.sales_manager), '') AS `менеджер`,
    'Контекст' AS `источник`,
    'Отзывы' AS `направление`,
    concat(toString(ifNull(r.`CampaignId`, 0)), '|', ifNull(r.`CampaignName`, '')) AS `номер кампании | название кампании`,
    concat(toString(ifNull(r.`AdGroupId`, 0)), '|', ifNull(r.`AdGroupName`, '')) AS `номер группы | название группы`,
    CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
    CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
    concat(ifNull(r.login, ''), '|', {domain_expr}) AS `аккаунт|сайт`,
    CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
    CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
    'Яндекс' AS `поставщик`,
    'direct_account_reviews' AS _source_table,
    CAST(NULL, 'Nullable(String)') AS cascade_level,
    CAST(NULL, 'Nullable(String)') AS campaign_status,
    CAST(NULL, 'Nullable(String)') AS payment_model
FROM ad_analytics.yandex_direct_reports_reviews r
LEFT JOIN accounts a ON a.account = r.login
LEFT JOIN gs ON gs.salon_key = lower(trim(ifNull(a.`салон`, '')))
"""


def _insert_reviews_rows(client, target_table: str) -> int:
    """Inserts into the shared source-store shadow (already holds direct/crop/... rows from
    earlier in step3).

    ROW_COUNT_BLIND_TO_FANOUT_FIX_2026-08-24: counting only the source table
    (`yandex_direct_reports_reviews`) tells you its SIZE, not how many rows the insert
    actually wrote — that's exactly how the original fan-out bug (see `_build_reviews_sql`
    docstring) went unnoticed: the log kept printing the source count while
    `big_analytics_full` silently inflated. So count `target_table` rows for this insert
    (`_source_table = 'direct_account_reviews'`, same pattern as
    `_insert_perform_vk_from_raw_leads`) and FAIL LOUDLY if it diverges from the source
    count — the two are supposed to be 1:1 (`accounts`/`gs` in `_build_reviews_sql` are
    both deduped to one row per key before the LEFT JOIN).
    """
    select_sql = _select_from_create(_build_reviews_sql(target_table.rsplit(".", 1)[-1]))
    client = _command_with_retry(
        client,
        f"INSERT INTO {target_table}\n{select_sql}",
        label="reviews insert",
        settings=STEP3_QUERY_SETTINGS,
    )
    inserted = int(
        client.query(
            f"SELECT count() FROM {target_table} WHERE _source_table = 'direct_account_reviews'",
            settings=STEP3_QUERY_SETTINGS,
        ).result_rows[0][0]
    )
    source_rows = int(
        client.query(
            "SELECT count() FROM ad_analytics.yandex_direct_reports_reviews",
            settings=STEP3_QUERY_SETTINGS,
        ).result_rows[0][0]
    )
    if inserted != source_rows:
        raise RuntimeError(
            f"reviews dedupe fan-out regression: inserted {inserted:,} rows into "
            f"{target_table} but source ad_analytics.yandex_direct_reports_reviews has "
            f"{source_rows:,} rows. argMax(col, id) GROUP BY `аккаунт` in the `accounts` "
            "CTE (or `gs` GROUP BY salon_key) is no longer deduping 1:1 — the LEFT JOIN "
            "in _build_reviews_sql is fanning out again."
        )
    return inserted


def _perform_vk_insert_sql(target_table: str) -> str:
    columns = _lead_source_columns("'VK Ads'", "'Перформ'", "ВК Реклама", "'vk_perform'")
    overrides = {
        "key3": "concat('vk_perform|', ifNull(l.salon, ''), '|', toString(l.created_date), '|', toString(l.id))",
        "CampaignId": "CAST(NULL, 'Nullable(Int64)')",
        "CampaignName": "'victory'",
        "AdGroupId": "CAST(NULL, 'Nullable(Int64)')",
        "AdGroupName": "CAST(NULL, 'Nullable(String)')",
        "AdNetworkType": "CAST(NULL, 'Nullable(String)')",
        "Device": "CAST(NULL, 'Nullable(String)')",
        "domain": "pvs.domain",
        "RlAdjustmentId": "CAST(NULL, 'Nullable(Int64)')",
        "RlAdjustmentId_total": "CAST(NULL, 'Nullable(String)')",
        "campaign_code": "CAST(NULL, 'Nullable(String)')",
        "tp": "CAST(NULL, 'Nullable(String)')",
        "cpc_cpa": "CAST(NULL, 'Nullable(String)')",
        "site_quiz": "CAST(NULL, 'Nullable(String)')",
        "adgroup_code": "CAST(NULL, 'Nullable(String)')",
        "account_login": "pvs.login_key",
        "manager_login": "CAST(NULL, 'Nullable(String)')",
        "ag_part1": "CAST(NULL, 'Nullable(String)')",
        "ag_part2": "CAST(NULL, 'Nullable(String)')",
        "ag_part3": "CAST(NULL, 'Nullable(String)')",
        "ag_part4": "CAST(NULL, 'Nullable(String)')",
        "ag_part5": "CAST(NULL, 'Nullable(String)')",
        "ag_part6": "CAST(NULL, 'Nullable(String)')",
        "ag_part7": "CAST(NULL, 'Nullable(String)')",
        "Название crm": "'crmf'",
        "тип_заявки": "'Заявки'",
        "статус": "CAST(NULL, 'Nullable(String)')",
        "специалист": "CAST(NULL, 'Nullable(String)')",
        "тип_сайта": "CAST(NULL, 'Nullable(String)')",
        "шаблон": "CAST(NULL, 'Nullable(String)')",
        "салон": "coalesce(nullIf(l.salon, ''), 'Перформ РФ')",
        "город": "CAST(NULL, 'Nullable(String)')",
        "регион": "CAST(NULL, 'Nullable(String)')",
        "direction": "'Авто'",
        "fid": "CAST(NULL, 'Nullable(String)')",
        "проджект": "CAST(NULL, 'Nullable(String)')",
        "id_салона": "'avto_0415'",
        "менеджер": "CAST(NULL, 'Nullable(String)')",
        "номер кампании | название кампании": "'victory'",
        "номер группы | название группы": "CAST(NULL, 'Nullable(String)')",
        "аккаунт|сайт": "concat(ifNull(pvs.login_key, ''), '|', ifNull(pvs.domain, ''))",
        "поставщик": "'ВК Реклама'",
    }
    columns = [(alias, overrides.get(alias, expr)) for alias, expr in columns]
    select_list = ",\n    ".join(f"{expr} AS {q(alias)}" for alias, expr in columns)
    return f"""
INSERT INTO {target_table}
WITH
lead_scored AS
(
    SELECT
        l.*,
        {_metric_expr("l.status", "l.reason", "l.source_type", "l.salon")}
    FROM ad_analytics.raw_leads AS l
    WHERE {_perform_vk_filter("l.")}
      AND (l.deal_type IS NULL OR l.deal_type != 'Звонок')
      AND ifNull(l.created_date, toDate('1970-01-01')) >= toDate('2026-01-01')
),
perform_vk_site AS
(
    SELECT
        lowerUTF8(trim(ifNull(domain, ''))) AS domain,
        login_key
    FROM reference_data.gsheet_sites
    WHERE client_id = 'avto_0415'
      AND niche = 'Авто'
      AND ifNull(domain, '') != ''
    ORDER BY lowerUTF8(trim(ifNull(domain, '')))
    LIMIT 1
)
SELECT
    {select_list}
FROM lead_scored AS l
CROSS JOIN perform_vk_site AS pvs
"""


def _insert_perform_vk_from_raw_leads(client, target_table: str) -> tuple[object, int]:
    client = _command_with_retry(
        client,
        _perform_vk_insert_sql(target_table),
        label="perform_vk insert",
        settings=STEP3_QUERY_SETTINGS,
    )
    rows = client.query(
        f"SELECT count() FROM {target_table} WHERE _source_table = 'vk_perform'"
    ).result_rows[0][0]
    return client, int(rows)


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 3 v6_ch: сборка source marts ClickHouse батчами")
    client = get_client()
    t0 = time.perf_counter()

    total = 0
    parts: list[str] = []
    shadow = f"ad_analytics.{SOURCE_STORE}_new"

    # CODE_STATUS_CATEGORY_2026-08-06 — до любой тяжёлой работы: статус, который
    # проставляет патч step1, но у которого нет категории, обнуляет воронку молча.
    check_code_status_categories(client)

    crm_problems = check_crm_mapping_coverage(client)
    if crm_problems:
        parts.append("crm_mapping_missing=" + "|".join(crm_problems))

    t_stage = time.perf_counter()
    client, leads_rows = _rebuild_leads_deduped_stage(client)
    parts.append(f"{LEADS_DEDUPED_STAGE}={leads_rows:,}")
    logger.info("  rebuilt ad_analytics.%s: %s rows за %.1f сек", LEADS_DEDUPED_STAGE, f"{leads_rows:,}", time.perf_counter() - t_stage)

    direct_ranges = day_ranges("2026-01-01")
    direct_batches = [
        _select_from_create(_build_direct_sql(SOURCE_STORE, f"AND ry.`Date` >= toDate('{lo}') AND ry.`Date` < toDate('{hi}')"))
        for lo, hi in direct_ranges
    ]
    logger.info("  rebuild ad_analytics.%s (%d direct batches)", SOURCE_STORE, len(direct_batches))
    client = _command_with_retry(client, f"DROP TABLE IF EXISTS {shadow} SYNC", label=f"{SOURCE_STORE} shadow drop")
    client = _command_with_retry(
        client,
        _build_direct_sql(f"{SOURCE_STORE}_new", "AND 0"),
        label=f"{SOURCE_STORE} shadow create",
        settings=STEP3_QUERY_SETTINGS,
    )
    for idx, select_sql in enumerate(direct_batches, start=1):
        t_batch = time.perf_counter()
        client = _command_with_retry(
            client,
            f"INSERT INTO {shadow}\n{select_sql}",
            label=f"{SOURCE_STORE} batch {idx}/{len(direct_batches)}",
            settings=STEP3_QUERY_SETTINGS,
        )
        logger.info(
            "    batch %s %d/%d inserted за %.1f сек",
            SOURCE_STORE,
            idx,
            len(direct_batches),
            time.perf_counter() - t_batch,
        )
    parts.append("direct=inserted")

    lead_ranges = day_ranges("2026-01-01")

    # Билдеры принимают окно батча (lo, hi): веткам direct_unmatched/direct_zero
    # нужна не только дата лида, но и то же окно для anti-join к raw_yandex.
    lead_builders = [
        ("big_analytics_seo", lambda lo, hi: _build_seo_sql(_lead_date_filter(lo, hi))),
        ("big_analytics_crop_targeting", lambda lo, hi: _build_crop_sql_batched(_lead_date_filter(lo, hi))),
        # DIRECT_LEAD_BRANCHES_2026-08-05 + CASCADE_MATCH_2026-07-03
        ("direct_cascade", _build_direct_cascade_sql),
        ("direct_unmatched", _build_direct_unmatched_sql),
        ("direct_zero", _build_direct_zero_sql),
    ]
    for table, builder in lead_builders:
        t_table = time.perf_counter()
        batch_selects = [_select_from_create(builder(lo, hi)) for lo, hi in lead_ranges]
        logger.info("  append %s into ad_analytics.%s (%d batches)", table, SOURCE_STORE, len(batch_selects))
        for idx, select_sql in enumerate(batch_selects, start=1):
            t_batch = time.perf_counter()
            client = _command_with_retry(
                client,
                f"INSERT INTO {shadow}\n{select_sql}",
                label=f"{table} batch {idx}/{len(batch_selects)}",
                settings=STEP3_QUERY_SETTINGS,
            )
            logger.info(
                "    batch %s %d/%d inserted за %.1f сек",
                table,
                idx,
                len(batch_selects),
                time.perf_counter() - t_batch,
            )
        parts.append(f"{table}=inserted")
        logger.info("  %s inserted за %.1f сек", table, time.perf_counter() - t_table)

    t_perform_vk = time.perf_counter()
    client, perform_vk_rows = _insert_perform_vk_from_raw_leads(client, shadow)
    parts.append(f"vk_perform={perform_vk_rows:,}")
    logger.info("  vk_perform inserted за %.1f сек: %s rows", time.perf_counter() - t_perform_vk, f"{perform_vk_rows:,}")

    t_reviews = time.perf_counter()
    reviews_rows = _insert_reviews_rows(client, shadow)
    parts.append(f"reviews={reviews_rows:,}")
    logger.info("  reviews inserted за %.1f сек: %s rows", time.perf_counter() - t_reviews, f"{reviews_rows:,}")

    _swap_shadow(client, SOURCE_STORE)
    recreate_source_views(client)

    source_rows = count_rows(client, f"ad_analytics.{SOURCE_STORE}")
    rows = count_rows(client, "ad_analytics.big_analytics_reviews")
    parts.append(f"big_analytics_reviews={rows:,}")
    parts.append(f"{SOURCE_STORE}={source_rows:,}")
    total = source_rows + rows

    client.command(f"DROP TABLE IF EXISTS ad_analytics.{LEADS_DEDUPED_STAGE} SYNC")
    logger.info("  cleaned ad_analytics.%s staging", LEADS_DEDUPED_STAGE)

    details = ", ".join(parts)
    logger.info("Шаг 3 v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": total, "details": details}


def get_explain_sql(conn=None) -> str:  # noqa: ARG001
    return f"SELECT count() FROM ad_analytics.{SOURCE_STORE}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
