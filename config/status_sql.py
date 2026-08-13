"""
config/status_sql.py — динамическая генерация SQL квалификации лидов
из таблицы local_crm_statuses.

Структура local_crm_statuses:
  crm_status  TEXT  — значение статуса/причины в таблице leads
  lead_status TEXT  — 'correct' | 'visit' | 'sale' | 'incorrect' | 'credit' | 'approved'
  crm_name    TEXT  — '' = общий; '<source_type>' = переопределение для конкретной CRM
  salon       TEXT  — '' = общий; '<salon>' = переопределение для конкретного салона
  kind        TEXT  — 'status' = колонка status; 'reason' = колонка reason (пока не активна)

Фиксированные (всегда хардкод, не из БД):
  ne_otvechaet — 'Не отвечает', 'Новая: Не отвечает'
  filtr        — 'Фильтр'
  nedozvon     — 'Недозвон'

kval берётся из отдельной категории qualified.

credit/approved берутся из БД (category='credit'/'approved').
"""

from __future__ import annotations

from collections import defaultdict

from config.settings import T_CRM_STATUSES

# Переключить в True когда в local_leads появится колонка reason
HAS_REASON_COLUMN = True

# Маппинг crm_statuses.crm → local_leads.source_type
# PATCH-CRM-SOURCE-MAP-2026-06-15: добавлены mauto и crmf
# PATCH-CRM-SOURCE-MAP-PLEX-2026-07-15: 'PLEX'→'plex' — ключ должен совпадать с
# фактическим регистром crm_name в local_crm_statuses ('plex', 8 строк, lowercase).
# С 'PLEX' (uppercase) .get() не матчил → source_type фолбэчил на 'plex' и не совпадал
# с реальным leads.source_type='plex_excel' → все 8 plex-override'ов были мёртвыми.
# (Регистр совпадает с уже-lowercase 'mauto'/'crmf'; 'MEGA' остаётся uppercase — так в данных.)
_CRM_TO_SOURCE_TYPE: dict[str, str] = {
    'MEGA':    'mega_crm_excel',
    'plex':    'plex_excel',
    'mauto':   'mauto_excel',
    'crmf':    'crmf_excel',
    'default': '',
}

_NE_OTVECHAET = ("'Не отвечает'", "'Новая: Не отвечает'")
_FILTR        = ("'Фильтр'",)
_NEDOZVON     = ("'Недозвон'",)

# PATCH-STATUS-SQL-DEMOTE-2026-07-13
# Ключ в by_cat, под которым хранится карта cross-category demotion-исключений
# salon-override'ов (см. _group_by_category / _demotion_excl). Служебный, не категория.
_DEMOTIONS_KEY = '__salon_demotions__'

# Status-сторона: рёбра вложенности категорий (src ⊆ dst).
# ⚠️ Должны ЗЕРКАЛИТЬ status-блок _merge()-вызовов в _group_by_category — если
# меняешь один, синхронизируй второй. Используются для вычисления «досягаемости»
# (reach) категории при cross-category demotion-исключении salon-override'ов.
_STATUS_MERGE_EDGES = (
    ('sale', 'approved'), ('approved', 'credit'), ('credit', 'visit'),
    ('sale', 'visit'), ('sale', 'qualified'), ('visit', 'qualified'),
    ('qualified', 'correct'), ('visit', 'correct'), ('sale', 'correct'),
)


def _status_reach(category: str) -> set[str]:
    """Множество категорий, в которые статус из `category` попадает по merge-каскаду
    (включая саму категорию). Пример: reach('credit') = {credit, visit, qualified, correct},
    reach('qualified') = {qualified, correct}."""
    seen = {category}
    frontier = [category]
    while frontier:
        cur = frontier.pop()
        for src, dst in _STATUS_MERGE_EDGES:
            if src == cur and dst not in seen:
                seen.add(dst)
                frontier.append(dst)
    return seen


def _demotion_excl(by_cat, category: str, kind_filter, s_col: str, d_col: str, t_col: str) -> str:
    """SQL-фрагмент ` AND NOT (...)`, вычитающий из general-ветки категории лиды салона,
    которые salon-override перенёс в другую, НЕ покрывающую эту категорию, ветку
    (кросс-категорийный аналог within-category `salon NOT IN (...)`). '' если исключать нечего.
    Только status-метрики: для kind_filter='reason' возвращает ''."""
    if kind_filter == 'reason':
        return ''
    cat_dem = by_cat.get(_DEMOTIONS_KEY, {}).get(category)
    if not cat_dem:
        return ''
    neg = []
    for (src_type, salon), statuses in sorted(cat_dem.items()):
        q = ', '.join(f"'{s}'" for s in sorted(statuses))
        if src_type:
            neg.append(f"({d_col} = '{salon}' AND {t_col} = '{src_type}' AND {s_col} IN ({q}))")
        else:
            neg.append(f"({d_col} = '{salon}' AND {s_col} IN ({q}))")
    # COALESCE(..., FALSE) — NULL-safe: при salon IS NULL предикат = NULL, а `NOT NULL`
    # в трёхзначной логике роняет лид (TRUE AND NOT(NULL)=NULL). Приводим NULL→FALSE,
    # чтобы general-ветка НЕ теряла лиды с salon IS NULL (не относящиеся к override-салонам).
    return ' AND NOT COALESCE((' + ' OR '.join(neg) + '), FALSE)'


def _load_raw(conn):
    """Загрузить все строки из local_crm_statuses с маппингом crm → source_type."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT crm_status,
                   lead_status,
                   COALESCE(TRIM(crm_name), '') AS crm_name,
                   COALESCE(TRIM(salon),    '') AS salon,
                   COALESCE(kind, 'status')     AS kind
            FROM {T_CRM_STATUSES}
            ORDER BY crm_name, lead_status, crm_status
        """)
        rows = cur.fetchall()
    result = []
    seen: set = set()
    for crm_status, lead_status, crm_name, salon, kind in rows:
        mapped_crm = _CRM_TO_SOURCE_TYPE.get(crm_name, crm_name)
        # reason-маппинги сравниваются через LOWER(reason) в SQL → значения тоже lowercase
        norm_status = crm_status.lower() if kind == 'reason' else crm_status
        key = (norm_status, lead_status, mapped_crm, salon, kind)
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _group_by_category(rows):
    """
    Вернуть: {lead_status: {(crm_name, salon, kind): [statuses]}}

    Автомердж (инварианты воронки):
      status-сторона:
        approved → credit → visit → qualified → correct
        sale → visit → qualified → correct (напрямую)
      reason-сторона (отдельно от status):
        approved → credit
        sale-reason → approved → credit (если sale в reason)

    qualified — отдельная категория для kval (НЕ удалена).
    Слияние происходит ВНУТРИ kind (status в status, reason в reason).
    """
    result = defaultdict(lambda: defaultdict(list))
    for crm_status, lead_status, crm_name, salon, kind in rows:
        result[lead_status][(crm_name, salon, kind)].append(crm_status)

    def _merge(src_cat: str, dst_cat: str) -> None:
        """Скопировать все ключи (crm, salon, kind) из src_cat в dst_cat без дублей.
        Также propagate general src статусы в CRM-override dst ключи,
        чтобы инварианты voronki (visit⊆qualified⊆correct) выполнялись для CRM-лидов."""
        for key, statuses in result.get(src_cat, {}).items():
            existing = set(result[dst_cat][key])
            for s in statuses:
                if s not in existing:
                    result[dst_cat][key].append(s)
                    existing.add(s)
        for (src_crm, src_salon, src_kind), src_statuses in result.get(src_cat, {}).items():
            if src_crm or src_salon:
                continue
            for (dst_crm, dst_salon, dst_kind) in list(result[dst_cat].keys()):
                if not dst_crm and not dst_salon:
                    continue
                if dst_kind != src_kind:
                    continue
                existing = set(result[dst_cat][(dst_crm, dst_salon, dst_kind)])
                for s in src_statuses:
                    if s not in existing:
                        result[dst_cat][(dst_crm, dst_salon, dst_kind)].append(s)
                        existing.add(s)

    # Status-сторона: sale ⊆ approved ⊆ credit ⊆ visit ⊆ qualified ⊆ correct
    _merge('sale',      'approved')
    _merge('approved', 'credit')    # approved status ⊆ credit status
    _merge('credit',   'visit')     # credit status ⊆ visit status
    _merge('sale',      'visit')
    _merge('sale',      'qualified')
    _merge('visit',     'qualified')
    _merge('qualified', 'correct')
    _merge('visit',     'correct')
    _merge('sale',      'correct')

    # Reason-сторона: sale ⊆ approved ⊆ credit
    _merge('sale',      'approved')
    _merge('approved',  'credit')

    # --- Cross-category DEMOTION salon-override'ов (PATCH-STATUS-SQL-DEMOTE-2026-07-13) ---
    # salon-override может переносить статус в БОЛЕЕ МЕЛКУЮ (менее глубокую) категорию,
    # чем default-строка. merge-каскад умеет только PROMOTE (объединение вверх), но не
    # DEMOTE: default-строка продолжает засчитывать лид салона в свою general-ветку тех
    # категорий, куда override его уже НЕ относит → задвоение (лид в двух категориях).
    # Считаем по СЫРЫМ rows (до inflation merge'ем), из каких категорий general-ветка должна
    # ИСКЛЮЧИТЬ лиды салона: {category: {(source_type, salon): {crm_status, ...}}}. Только kind='status'.
    default_cat: dict[str, str] = {}
    for crm_status, lead_status, crm_name, salon, kind in rows:
        if kind == 'status' and not crm_name and not salon:
            default_cat[crm_status] = lead_status
    demotions: dict = defaultdict(lambda: defaultdict(set))
    for crm_status, lead_status, crm_name, salon, kind in rows:
        if kind != 'status' or not salon:
            continue
        def_cat = default_cat.get(crm_status)
        if def_cat is None:
            continue  # нет general-default → из general-ветки нечего исключать
        src_type = _CRM_TO_SOURCE_TYPE.get(crm_name, crm_name)
        for leaked_cat in _status_reach(def_cat) - _status_reach(lead_status):
            demotions[leaked_cat][(src_type, salon)].add(crm_status)
    result[_DEMOTIONS_KEY] = demotions

    return result


def _extract_cond_parts(by_cat, category: str, kind_filter: str | None = None):
    """
    Вернуть (general_statuses, general_reasons, overrides_dict, salon_overrides_dict).

    kind_filter:
      - 'status' — только status-маппинги (general_reason = [])
      - 'reason' — только reason-маппинги (general = [], overrides = {}, salon_overrides = {})
      - None     — все (старое поведение, не используется в новой воронке)

    general_statuses    — kind='status', crm_name='', salon='' (status-колонка)
    general_reasons     — kind='reason', crm_name='', salon='' (reason-колонка)
    overrides_dict      — {src_type: [statuses]} переопределения по CRM без salon, kind='status'
    salon_overrides_dict — {(src_type, salon_val): [statuses]} переопределения по CRM+salon
                           (src_type может быть '' если crm_name не задан)
    """
    cat_data = by_cat.get(category, {})

    if kind_filter == 'reason':
        general_reason = cat_data.get(('', '', 'reason'), [])
        return [], general_reason, {}, {}

    general        = cat_data.get(('', '', 'status'), [])
    general_reason = cat_data.get(('', '', 'reason'), []) if kind_filter is None else []
    overrides: dict[str, list] = {}
    salon_overrides: dict[tuple, list] = {}
    for (crm_name, salon, kind), statuses in cat_data.items():
        if kind != (kind_filter or kind):
            continue
        if salon:
            # Специфичные по салону (наиболее приоритетные)
            src_type = _CRM_TO_SOURCE_TYPE.get(crm_name, crm_name) if crm_name else ''
            key = (src_type, salon)
            existing = set(salon_overrides.get(key, []))
            salon_overrides[key] = salon_overrides.get(key, []) + [s for s in statuses if s not in existing]
        elif crm_name and kind == 'status':
            overrides[crm_name] = statuses
    return general, general_reason, overrides, salon_overrides


def _case_expr(by_cat, category: str, alias: str, prefix: str = '',
               else_val: str = '0', kind_filter: str | None = None,
               extra_cond: str | None = None) -> str:
    """
    Сгенерировать: CASE WHEN ... THEN 1 ELSE {else_val} END AS {alias}

    prefix: '' для leads-запросов, 'c.' для raw_calls (алиас c.)
    else_val: '0' для булевых метрик, 'NULL' для суммируемых (dohod/dobro)
    kind_filter: 'status' / 'reason' / None — какие маппинги использовать
    extra_cond: дополнительное OR-условие (напр. reason IN credit+approved)
    """
    s_col = f'{prefix}status'
    r_col = f'{prefix}reason'
    t_col = f'{prefix}source_type'
    d_col = f'{prefix}salon'

    general, general_reason, overrides, salon_overrides = _extract_cond_parts(by_cat, category, kind_filter)

    if not general and not general_reason and not overrides and not salon_overrides and not extra_cond:
        return f'{else_val} AS {alias}'

    # Cross-category demotion: вычесть из general-ветки лиды салонов, чей override
    # перенёс статус в НЕ покрывающую эту категорию ветку (PATCH-STATUS-SQL-DEMOTE-2026-07-13).
    xcl = _demotion_excl(by_cat, category, kind_filter, s_col, d_col, t_col)

    parts = []
    # salon_overrides — наиболее специфичные, идут первыми
    if salon_overrides:
        for (src_type, salon_val), statuses in sorted(salon_overrides.items()):
            quoted = ', '.join(f"'{s}'" for s in statuses)
            if src_type:
                parts.append(f"({d_col} = '{salon_val}' AND {t_col} = '{src_type}' AND {s_col} IN ({quoted}))")
            else:
                parts.append(f"({d_col} = '{salon_val}' AND {s_col} IN ({quoted}))")
    if overrides:
        for src_type, statuses in sorted(overrides.items()):
            quoted = ', '.join(f"'{s}'" for s in statuses)
            parts.append(f"({t_col} = '{src_type}' AND {s_col} IN ({quoted}))")
        if general:
            # PATCH-STATUS-SQL-EXCL-2026-06-15: salon_overrides убраны из excl_set —
            # salon-ветка уже исключает своим (salon=X AND source_type=st AND ...) условием,
            # влив в excl обнулял korr/kval у crmf/mauto-салонов кроме «Лидер»
            excl_set = set(overrides.keys())
            excl   = ', '.join(f"'{k}'" for k in sorted(excl_set))
            quoted = ', '.join(f"'{s}'" for s in general)
            parts.append(
                f"(({t_col} IS NULL OR {t_col} NOT IN ({excl})) AND {s_col} IN ({quoted}){xcl})"
            )
    else:
        if general:
            if salon_overrides:
                # Исключаем салоны покрытые salon_overrides для general
                excl_salons = ', '.join(f"'{sv}'" for (_, sv) in sorted(salon_overrides))
                quoted = ', '.join(f"'{s}'" for s in general)
                parts.append(
                    f"(({d_col} IS NULL OR {d_col} NOT IN ({excl_salons})) AND {s_col} IN ({quoted}){xcl})"
                )
            else:
                quoted = ', '.join(f"'{s}'" for s in general)
                parts.append(f"({s_col} IN ({quoted}){xcl})" if xcl else f"{s_col} IN ({quoted})")

    if general_reason:
        quoted = ', '.join(f"'{r}'" for r in general_reason)
        parts.append(f"LOWER({r_col}) IN ({quoted})")

    if extra_cond:
        parts.append(extra_cond)

    cond = '\n        OR '.join(parts)
    return f'CASE WHEN {cond} THEN 1 ELSE {else_val} END AS {alias}'


def _ca_reason_cond(by_cat: dict, r_col: str) -> str | None:
    """
    OR-условие reason IN (credit+approved reason-статусы) для каскада в status-воронку.
    Лиды с таким reason считаются в korr/kval/priezd/kol_vo_zayavok даже если
    leads.status не замаплен. None если таблица пуста.
    """
    vals: set[str] = set()
    for cat in ('credit', 'approved'):
        for (crm_name, salon, kind), statuses in by_cat.get(cat, {}).items():
            if kind == 'reason' and not crm_name and not salon:
                vals.update(statuses)
    if not vals:
        return None
    quoted = ', '.join(f"'{v}'" for v in sorted(vals))
    return f"{r_col} IN ({quoted})"


def _priezd_statuses_in(by_cat) -> str:
    """
    Список visit+sale STATUS-значений (kind='status') для дедупликации.
    Reason-значения намеренно исключены — дедуп работает только по status-колонке.
    """
    all_s = set()
    for cat in ('visit', 'sale'):
        for (crm_name, salon, kind), statuses in by_cat.get(cat, {}).items():
            if kind == 'status':
                all_s.update(statuses)
    return ', '.join(f"'{s}'" for s in sorted(all_s)) if all_s else 'NULL'


def _build_status_cases(by_cat, prefix: str = '') -> str:
    """
    Строка из 12 CASE-выражений через запятую для SELECT.

    Логика:
      - korr / kval / priezd / prodazhi / nekorr — ТОЛЬКО kind='status'
      - dohod_do_kredita / dobro — ТОЛЬКО kind='reason'
      - kval — отдельная категория `qualified`
      - reason НЕ примешивается к status-метрикам (иначе двойной счёт: лид
        со status='Корзина' + reason='Купил' даёт и nekorr=1 и korr=1)
    """
    s = f'{prefix}status'
    ne_in  = ', '.join(_NE_OTVECHAET)
    fi_in  = ', '.join(_FILTR)
    ned_in = ', '.join(_NEDOZVON)
    return (
        f"    CASE WHEN ({s} IS NOT NULL AND {s} != '') THEN 1 ELSE 0 END AS kol_vo_zayavok,\n"
        f"    {_case_expr(by_cat, 'correct',   'korr',             prefix, kind_filter='status')},\n"
        f"    {_case_expr(by_cat, 'qualified', 'kval',             prefix, kind_filter='status')},\n"
        f"    {_case_expr(by_cat, 'visit',     'priezd',           prefix, kind_filter='status')},\n"
        f"    {_case_expr(by_cat, 'sale',      'prodazhi',         prefix, kind_filter='status')},\n"
        f"    {_case_expr(by_cat, 'incorrect', 'nekorr',           prefix, kind_filter='status')},\n"
        f"    CASE WHEN {s} IN ({ne_in}) THEN 1 ELSE 0 END AS ne_otvechaet,\n"
        f"    CASE WHEN {s} IN ({fi_in}) THEN 1 ELSE 0 END AS filtr,\n"
        f"    CASE WHEN {s} IN ({ned_in}) THEN 1 ELSE 0 END AS nedozvon,\n"
        f"    CASE WHEN {s} = 'Приедет' THEN 1 ELSE 0 END AS priedet,\n"
        f"    {_case_expr(by_cat, 'credit',   'dohod_do_kredita', prefix, else_val='NULL', kind_filter='reason')},\n"
        f"    {_case_expr(by_cat, 'approved', 'dobro',            prefix, else_val='NULL', kind_filter='reason')}"
    )


def _build_calls_agg(by_cat, alias_salon: str = 'gs."salon"', alias_source: str = 'c.source_type') -> str:
    """
    Строка COALESCE(SUM(CASE WHEN...), 0) AS metric для GROUP BY звонков.
    Использует алиас 'c.' (raw_calls c) для status/reason.

    ВАЖНО (путь звонков — _add_crop_calls_sql):
      - salon берётся из JOIN gsheet_sites (gs."salon"), т.к. raw_calls (c) НЕ имеет
        колонки salon — ссылка на c.salon валит шаг 3 с 'column c.salon does not exist'.
      - source_type для salon_overrides — СЫРОЙ (crmf_excel/plex_excel/...), а не маппленый
        лейбл (Фаиг/Плекс). raw_calls.source_type уже сырой → alias_source='c.source_type'.
        НЕ использовать dst.leads_source_type здесь: это маппленый лейбл, он сломает матч
        ключа salon_override (ключ = сырой source_type).

    Параметры alias_salon / alias_source оставлены настраиваемыми на случай иного контекста
    вызова, но дефолты соответствуют SQL-окружению _add_crop_calls_sql (FROM raw_calls c
    LEFT JOIN gsheet_sites gs).

    Новая логика (status/reason разделение):
      - korr/kval/priezd/prodazhi/nekorr — kind='status'
      - dohod_do_kredita/dobro — kind='reason'
      - kval — отдельная категория `qualified` (не формула)
    """
    s = 'c.status'
    r = 'LOWER(c.reason)'
    t = alias_source
    d = alias_salon
    ne_in  = ', '.join(_NE_OTVECHAET)
    fi_in  = ', '.join(_FILTR)
    ned_in = ', '.join(_NEDOZVON)

    def _cond(category, kind_filter=None):
        general, general_reason, overrides, salon_overrides = _extract_cond_parts(by_cat, category, kind_filter)
        if not general and not general_reason and not overrides and not salon_overrides:
            return '1=0'
        parts = []
        # Cross-category demotion (PATCH-STATUS-SQL-DEMOTE-2026-07-13)
        xcl = _demotion_excl(by_cat, category, kind_filter, s, d, t)
        if salon_overrides:
            for (src_type, salon_val), statuses in sorted(salon_overrides.items()):
                q = ', '.join(f"'{x}'" for x in statuses)
                if src_type:
                    parts.append(f"({d} = '{salon_val}' AND {t} = '{src_type}' AND {s} IN ({q}))")
                else:
                    parts.append(f"({d} = '{salon_val}' AND {s} IN ({q}))")
        if overrides:
            for src_type, statuses in sorted(overrides.items()):
                q = ', '.join(f"'{x}'" for x in statuses)
                parts.append(f"({t} = '{src_type}' AND {s} IN ({q}))")
            if general:
                # PATCH-STATUS-SQL-EXCL-2026-06-15: salon_overrides убраны из excl_set —
                # salon-ветка уже исключает своим условием, влив обнулял korr/kval у crmf/mauto-салонов
                excl_set = set(overrides.keys())
                excl = ', '.join(f"'{k}'" for k in sorted(excl_set))
                q    = ', '.join(f"'{x}'" for x in general)
                parts.append(f"(({t} IS NULL OR {t} NOT IN ({excl})) AND {s} IN ({q}){xcl})")
        else:
            if general:
                if salon_overrides:
                    excl_salons = ', '.join(f"'{sv}'" for (_, sv) in sorted(salon_overrides))
                    q = ', '.join(f"'{x}'" for x in general)
                    parts.append(f"(({d} IS NULL OR {d} NOT IN ({excl_salons})) AND {s} IN ({q}){xcl})")
                else:
                    q = ', '.join(f"'{x}'" for x in general)
                    parts.append(f"({s} IN ({q}){xcl})" if xcl else f"{s} IN ({q})")
        if general_reason:
            q = ', '.join(f"'{x}'" for x in general_reason)
            parts.append(f"{r} IN ({q})")
        return ' OR '.join(parts)

    korr_c     = _cond('correct',   kind_filter='status')
    kval_c     = _cond('qualified', kind_filter='status')
    priezd_c   = _cond('visit',     kind_filter='status')
    prodazhi_c = _cond('sale',      kind_filter='status')
    nekorr_c   = _cond('incorrect', kind_filter='status')
    credit_c   = _cond('credit',    kind_filter='reason')
    approved_c = _cond('approved',  kind_filter='reason')

    # reason НЕ примешивается к status-метрикам — иначе двойной счёт
    # (лид status='Корзина' + reason='Купил' → nekorr=1 AND korr=1 одновременно)

    return (
        f"    COALESCE(SUM(CASE WHEN ({s} IS NOT NULL AND {s} != '') THEN 1 ELSE 0 END), 0)::NUMERIC AS kol_vo_zayavok,\n"
        f"    COALESCE(SUM(CASE WHEN {korr_c}     THEN 1 ELSE 0 END), 0)::NUMERIC AS korr,\n"
        f"    COALESCE(SUM(CASE WHEN {kval_c}     THEN 1 ELSE 0 END), 0)::NUMERIC AS kval,\n"
        f"    COALESCE(SUM(CASE WHEN {priezd_c}   THEN 1 ELSE 0 END), 0)::NUMERIC AS priezd,\n"
        f"    COALESCE(SUM(CASE WHEN {prodazhi_c} THEN 1 ELSE 0 END), 0)::NUMERIC AS prodazhi,\n"
        f"    COALESCE(SUM(CASE WHEN {nekorr_c}   THEN 1 ELSE 0 END), 0)::NUMERIC AS nekorr,\n"
        f"    COALESCE(SUM(CASE WHEN {s} IN ({ne_in}) THEN 1 ELSE 0 END), 0)::NUMERIC AS ne_otvechaet,\n"
        f"    COALESCE(SUM(CASE WHEN {s} IN ({fi_in}) THEN 1 ELSE 0 END), 0)::NUMERIC AS filtr,\n"
        f"    COALESCE(SUM(CASE WHEN {s} IN ({ned_in}) THEN 1 ELSE 0 END), 0)::NUMERIC AS nedozvon,\n"
        f"    COALESCE(SUM(CASE WHEN {s} = 'Приедет' THEN 1 ELSE 0 END), 0)::NUMERIC AS priedet,\n"
        f"    SUM(CASE WHEN {credit_c}   THEN 1 ELSE NULL END) AS dohod_do_kredita,\n"
        f"    SUM(CASE WHEN {approved_c} THEN 1 ELSE NULL END) AS dobro"
    )


def _build_leads_agg(by_cat, alias_salon: str = 'salon', visit_gate_sql: str | None = None) -> str:
    """
    COALESCE(SUM(CASE WHEN...)) aggregation для GROUP BY в CTE над raw_leads.
    Ссылается на колонки status / reason / source_type без table-prefix.

    Новая логика (status/reason разделение):
      - korr/kval/priezd/prodazhi/nekorr — kind='status'
      - dohod_do_kredita/dobro — kind='reason'
      - kval — отдельная категория `qualified` (не формула)

    alias_salon — имя salon-колонки в контексте подстановки. По умолчанию 'salon'
    (латиница, как в leads-CTE step3/step5, где источник несёт колонку `salon`).
    Параметр нужен, т.к. salon-override ветка (`salon_overrides`, kind='status')
    генерит условие `<salon> = '<val>'` — а в step13 leads_scored salon-колонка
    переименована в `"салон"` (кириллица). Без параметра step13 валится
    `column "salon" does not exist`, когда в local_crm_statuses появляется
    salon-override (латентная ветка, та же семья что фикс #11 _build_calls_agg).

    visit_gate_sql — MARCAR_STRICT_ARRIVAL_2026-07-17. Булев SQL-предикат, которым
    ДОПОЛНИТЕЛЬНО ограничиваются ТОЛЬКО priezd и prodazhi (визит/продажа не
    засчитываются, когда предикат FALSE). korr/kval/kol_vo_zayavok/nekorr и
    reason-метрики (dohod_do_kredita/dobro) НЕ трогаются — лид остаётся на оси
    со своими заявочными счётчиками.
    ⚠️ По умолчанию None → строка-результат БАЙТ-В-БАЙТ прежняя. Это критично:
    генератор общий с заявка-осью (step3/step6/step5/corrections), изменение
    по умолчанию поехало бы на golden Кудерко. Гейт передаёт ТОЛЬКО step13_arrival.
    Гейтим priezd И prodazhi ОДНИМ предикатом — иначе рвётся вложенность воронки
    sale ⊆ visit (_STATUS_MERGE_EDGES: ('sale','visit')).
    """
    s = 'status'
    r = 'LOWER(reason)'
    t = 'source_type'
    d = alias_salon
    ne_in  = ', '.join(_NE_OTVECHAET)
    fi_in  = ', '.join(_FILTR)
    ned_in = ', '.join(_NEDOZVON)

    def _cond(category, kind_filter=None):
        general, general_reason, overrides, salon_overrides = _extract_cond_parts(by_cat, category, kind_filter)
        if not general and not general_reason and not overrides and not salon_overrides:
            return '1=0'
        parts = []
        # Cross-category demotion (PATCH-STATUS-SQL-DEMOTE-2026-07-13)
        xcl = _demotion_excl(by_cat, category, kind_filter, s, d, t)
        if salon_overrides:
            for (src_type, salon_val), statuses in sorted(salon_overrides.items()):
                q = ', '.join(f"'{x}'" for x in statuses)
                if src_type:
                    parts.append(f"({d} = '{salon_val}' AND {t} = '{src_type}' AND {s} IN ({q}))")
                else:
                    parts.append(f"({d} = '{salon_val}' AND {s} IN ({q}))")
        if overrides:
            for src_type, statuses in sorted(overrides.items()):
                q = ', '.join(f"'{x}'" for x in statuses)
                parts.append(f"({t} = '{src_type}' AND {s} IN ({q}))")
            if general:
                # PATCH-STATUS-SQL-EXCL-2026-06-15: salon_overrides убраны из excl_set —
                # salon-ветка уже исключает своим условием, влив обнулял korr/kval у crmf/mauto-салонов
                excl_set = set(overrides.keys())
                excl = ', '.join(f"'{k}'" for k in sorted(excl_set))
                q    = ', '.join(f"'{x}'" for x in general)
                parts.append(f"(({t} IS NULL OR {t} NOT IN ({excl})) AND {s} IN ({q}){xcl})")
        else:
            if general:
                if salon_overrides:
                    excl_salons = ', '.join(f"'{sv}'" for (_, sv) in sorted(salon_overrides))
                    q = ', '.join(f"'{x}'" for x in general)
                    parts.append(f"(({d} IS NULL OR {d} NOT IN ({excl_salons})) AND {s} IN ({q}){xcl})")
                else:
                    q = ', '.join(f"'{x}'" for x in general)
                    parts.append(f"({s} IN ({q}){xcl})" if xcl else f"{s} IN ({q})")
        if general_reason:
            q = ', '.join(f"'{x}'" for x in general_reason)
            parts.append(f"{r} IN ({q})")
        return ' OR '.join(parts)

    korr_c     = _cond('correct',   kind_filter='status')
    kval_c     = _cond('qualified', kind_filter='status')
    priezd_c   = _cond('visit',     kind_filter='status')
    prodazhi_c = _cond('sale',      kind_filter='status')
    nekorr_c   = _cond('incorrect', kind_filter='status')
    credit_c   = _cond('credit',    kind_filter='reason')
    approved_c = _cond('approved',  kind_filter='reason')

    # reason НЕ примешивается к status-метрикам — иначе двойной счёт
    # (лид status='Корзина' + reason='Купил' → nekorr=1 AND korr=1 одновременно)

    # MARCAR_STRICT_ARRIVAL_2026-07-17: гейт визита. Сужаем ТОЛЬКО priezd/prodazhi;
    # korr/kval/kol_vo_zayavok/nekorr и reason-метрики остаются нетронутыми — лид
    # остаётся на визит-оси со своими заявочными счётчиками (решение Семёна).
    # Оба гейтим ОДНИМ предикатом: раздельный порвал бы вложенность sale ⊆ visit
    # (_STATUS_MERGE_EDGES). visit_gate_sql=None → ветка не выполняется → вывод
    # байт-в-байт прежний для заявка-оси (step3/step6/step5/corrections).
    if visit_gate_sql:
        priezd_c   = f"({visit_gate_sql}) AND ({priezd_c})"
        prodazhi_c = f"({visit_gate_sql}) AND ({prodazhi_c})"

    return (
        f"    COALESCE(SUM(CASE WHEN ({s} IS NOT NULL AND {s} != '') THEN 1 ELSE 0 END), 0)::NUMERIC AS kol_vo_zayavok,\n"
        f"    COALESCE(SUM(CASE WHEN {korr_c}     THEN 1 ELSE 0 END), 0)::NUMERIC AS korr,\n"
        f"    COALESCE(SUM(CASE WHEN {kval_c}     THEN 1 ELSE 0 END), 0)::NUMERIC AS kval,\n"
        f"    COALESCE(SUM(CASE WHEN {priezd_c}   THEN 1 ELSE 0 END), 0)::NUMERIC AS priezd,\n"
        f"    COALESCE(SUM(CASE WHEN {prodazhi_c} THEN 1 ELSE 0 END), 0)::NUMERIC AS prodazhi,\n"
        f"    COALESCE(SUM(CASE WHEN {nekorr_c}   THEN 1 ELSE 0 END), 0)::NUMERIC AS nekorr,\n"
        f"    COALESCE(SUM(CASE WHEN {s} IN ({ne_in}) THEN 1 ELSE 0 END), 0)::NUMERIC AS ne_otvechaet,\n"
        f"    COALESCE(SUM(CASE WHEN {s} IN ({fi_in}) THEN 1 ELSE 0 END), 0)::NUMERIC AS filtr,\n"
        f"    COALESCE(SUM(CASE WHEN {s} IN ({ned_in}) THEN 1 ELSE 0 END), 0)::NUMERIC AS nedozvon,\n"
        f"    COALESCE(SUM(CASE WHEN {s} = 'Приедет' THEN 1 ELSE 0 END), 0)::NUMERIC AS priedet,\n"
        f"    SUM(CASE WHEN {credit_c}   THEN 1 ELSE NULL END) AS dohod_do_kredita,\n"
        f"    SUM(CASE WHEN {approved_c} THEN 1 ELSE NULL END) AS dobro"
    )


def load_status_sql(conn, leads_alias_salon: str = 'salon',
                    visit_gate_sql: str | None = None) -> dict:
    """
    Загрузить статусы из local_crm_statuses и вернуть SQL-фрагменты.

    Возвращает:
      status_cases        — 11 CASE-выражений для SELECT в leads-запросах
      calls_agg_cases     — 11 COALESCE(SUM(...)) для GROUP BY звонков
      leads_agg_cases     — 11 COALESCE(SUM(...)) для GROUP BY лидов (без table-prefix)
      priezd_statuses_sql — IN-список visit+sale статусов (для дедупликации)

    leads_alias_salon — имя salon-колонки для leads-агрегата. По умолчанию 'salon'
    (латиница, step3/step5). step13_arrival передаёт '"салон"' (кириллица), т.к. его
    leads_scored CTE переименовывает salon → "салон" (иначе salon-override ветка
    валит шаг при появлении salon-override в local_crm_statuses).

    visit_gate_sql — булев SQL-предикат, сужающий ТОЛЬКО priezd/prodazhi в
    leads_agg_cases (MARCAR_STRICT_ARRIVAL_2026-07-17). None (по умолчанию) →
    вывод байт-в-байт прежний. Передаёт только step13_arrival (визит-ось);
    заявка-ось (step3/step6/step5/corrections) зовёт без него → golden не затронут.
    Подробности — в docstring _build_leads_agg.
    """
    rows   = _load_raw(conn)
    by_cat = _group_by_category(rows)

    return {
        'status_cases':        _build_status_cases(by_cat, prefix=''),
        'calls_agg_cases':     _build_calls_agg(by_cat),
        'leads_agg_cases':     _build_leads_agg(by_cat, alias_salon=leads_alias_salon,
                                                visit_gate_sql=visit_gate_sql),
        'priezd_statuses_sql': _priezd_statuses_in(by_cat),
    }


def build_leads_agg_sql(conn) -> str:
    """Публичный алиас — загружает статусы и возвращает leads_agg_cases."""
    rows   = _load_raw(conn)
    by_cat = _group_by_category(rows)
    return _build_leads_agg(by_cat)
