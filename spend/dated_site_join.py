"""Date-aware match of `account_login` to its `reference_data.gsheet_sites` row.

Shared by the three Direct spend fact builders (`region_spend`, `criterion_spend`,
`adformat_spend`), which used to `LEFT JOIN reference_data.gsheet_sites gs ON
lower(gs.login_key) = lower(y.account_login)` with no date condition. A login is
sometimes reused for a second site launch later (measured live: 23 of 1106 active
logins have >1 `gsheet_sites` row), each with its own `launch_date`/`block_date`
window and its own `directologist`. The undated join fanned the JOIN out to every
matching row and the builders' GROUP BY (keyed on date/campaign/account_login, not
on the matched row) then summed `cost` once PER fanned row -- e.g. login
`e-20074386` carried 537,699.29 RUB of raw spend but landed at 1,075,398.57 in
`fact_region_spend` (exactly 2x, verified live 2026-08-25). `gs_best` below picks
AT MOST ONE `gsheet_sites` row per (account_login, date), which removes the
fan-out and, as a side effect, resolves the correct site for the period.

Priority mirrors the proven pattern already used for the SAME table on the claim
axis (`step3_build_sources/step3.py::_build_direct_sql` `gs_best` CTE,
`step6_build_full/step6.py` mirrors it) -- reused verbatim, not reinvented:
  1 = inside [launch_date, block_date) (open-ended if either bound is blank)
  2 = the row carries no date info at all (launch_date AND block_date both blank)
  3 = the row has date info but this date falls outside every window it covers
      (kept, not dropped -- a spend row must never disappear for lack of a date
      match; ties broken toward the most recently launched row)
  99 = account_login has no `gsheet_sites` row at all (site_key stays 0 downstream,
       same behaviour as before this change)
"""

from __future__ import annotations


def gs_best_cte(pairs_sql: str) -> str:
    """`WITH` fragment (no trailing comma) — call inside a `WITH ..., {gs_best_cte(...)}`.

    `pairs_sql` must yield exactly two columns: `login_key` (lowercased, trimmed
    `account_login`) and `date_val` (Date). Result `gs_best` has one row per
    (login_key, date_val) with `domain`/`directologist` from the best-matching
    `gsheet_sites` row, ready to LEFT JOIN back onto the driving table.
    """
    return f"""
    gs_best AS
    (
        SELECT * FROM
        (
            SELECT
                ud.login_key AS match_login_key,
                ud.date_val AS match_date,
                gs.domain AS domain,
                gs.directologist AS directologist,
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
            FROM ({pairs_sql}) ud
            LEFT JOIN reference_data.gsheet_sites gs
              ON lower(trim(ifNull(gs.login_key, ''))) = ud.login_key
        )
        WHERE rn = 1
    )
    """


def _demo() -> None:
    """Assert-based self-check (SQL shape only -- the join semantics are proven
    live against Victory ClickHouse, see the task report, not here)."""
    sql = gs_best_cte("SELECT login_key, date_val FROM x")
    assert sql.count("(") == sql.count(")"), "unbalanced parens"
    for token in ("gs_best", "match_priority", "row_number() OVER", "rn = 1",
                  "launch_date", "block_date", "match_login_key", "match_date"):
        assert token in sql, f"missing {token!r}"
    print("dated_site_join self-check OK")


if __name__ == "__main__":
    _demo()
