-- Mapping for Yandex Direct tp8/tp9/tp10 placements to known Telegram channel links.
-- Source placement: raw_data.yandex_direct_report_rows.placement.
-- Source link: ad_analytics.local_telega_in_orders.channel_link.
--
-- The link is filled only when the Telega.in match is unambiguous at the best
-- available priority. Ambiguous title matches stay NULL instead of storing a
-- guessed channel link.

DROP TABLE IF EXISTS ad_analytics.yandex_direct_tp_placement_links SYNC;

CREATE TABLE ad_analytics.yandex_direct_tp_placement_links
ENGINE = MergeTree
ORDER BY placement
AS
WITH
placements AS (
    SELECT
        trim(ifNull(placement, '')) AS placement,
        lowerUTF8(trim(ifNull(placement, ''))) AS placement_norm
    FROM raw_data.yandex_direct_report_rows
    WHERE match(ifNull(campaign_name, ''), '(?i)tp(8|9|10)')
      AND ifNull(placement, '') != ''
    GROUP BY placement
),
links_agg AS (
    SELECT
        channel_link,
        argMax(channel_name, updated_at) AS channel_name,
        argMax(utm_campaign, updated_at) AS utm_campaign,
        argMax(order_project_name, updated_at) AS order_project_name
    FROM ad_analytics.local_telega_in_orders
    WHERE ifNull(channel_link, '') != ''
      AND (
          positionCaseInsensitive(channel_link, 't.me/') > 0
          OR positionCaseInsensitive(channel_link, 'telegram.me/') > 0
      )
    GROUP BY channel_link
),
links AS (
    SELECT
        channel_link,
        lowerUTF8(trim(ifNull(channel_name, ''))) AS channel_name_norm,
        lowerUTF8(trim(ifNull(utm_campaign, ''))) AS utm_campaign_norm,
        lowerUTF8(trim(ifNull(order_project_name, ''))) AS order_project_norm
    FROM links_agg
),
candidates AS (
    SELECT
        p.placement,
        l.channel_link,
        multiIf(
            p.placement_norm = l.channel_name_norm, 1,
            p.placement_norm = l.utm_campaign_norm, 2,
            length(p.placement_norm) >= 10
                AND (
                    position(p.placement_norm, ' ') > 0
                    OR position(p.placement_norm, '|') > 0
                    OR position(p.placement_norm, '-') > 0
                    OR position(p.placement_norm, '#') > 0
                )
                AND position(l.order_project_norm, p.placement_norm) > 0, 3,
            99
        ) AS match_priority
    FROM placements AS p
    INNER JOIN links AS l ON
        p.placement_norm = l.channel_name_norm
        OR p.placement_norm = l.utm_campaign_norm
        OR (
            length(p.placement_norm) >= 10
            AND (
                position(p.placement_norm, ' ') > 0
                OR position(p.placement_norm, '|') > 0
                OR position(p.placement_norm, '-') > 0
                OR position(p.placement_norm, '#') > 0
            )
            AND position(l.order_project_norm, p.placement_norm) > 0
        )
),
best_priority AS (
    SELECT
        placement,
        min(match_priority) AS best_match_priority
    FROM candidates
    GROUP BY placement
),
best_candidates AS (
    SELECT
        c.placement,
        c.channel_link
    FROM candidates AS c
    INNER JOIN best_priority AS b
        ON c.placement = b.placement
       AND c.match_priority = b.best_match_priority
),
safe_links AS (
    SELECT
        placement,
        any(channel_link) AS telegram_link
    FROM best_candidates
    GROUP BY placement
    HAVING count() = 1
)
SELECT
    p.placement AS placement,
    CAST(s.telegram_link, 'Nullable(String)') AS telegram_link
FROM placements AS p
LEFT JOIN safe_links AS s
    ON p.placement = s.placement
ORDER BY p.placement;
