"""Shared ClickHouse expression for Yandex Direct criterion normalization."""

CRITERION_CLEAN = """
trim(replaceRegexpAll(
    replaceRegexpAll(
        replaceRegexpAll(
            replaceAll(replaceAll(replaceAll(ifNull(criterion, ''), '\u00a0', ' '), '\u202f', ' '), '\u2009', ' '),
            '^-+',
            ''
        ),
        '\\\\s+-.*$', ''
    ),
    '[!+\\\\[\\\\]]', ''
))
"""
