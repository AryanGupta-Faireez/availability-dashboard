#!/usr/bin/env python3
"""Download availability data from DB → data/availability.csv"""

import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

_DB = dict(
    host=os.environ.get("DB_HOST", "faireez-db.ceaaeaabvoqy.us-east-1.rds.amazonaws.com"),
    port=int(os.environ.get("DB_PORT", "5432")),
    dbname=os.environ.get("DB_NAME", "faireez"),
    user=os.environ.get("DB_USER", "postgres"),
    password=os.environ["DB_PASSWORD"],
)

AVAILABILITY_SQL = """
WITH
    current_and_next_weeks AS MATERIALIZED (
        SELECT
            "Id",
            "WeekNumber",
            "StartOfWeek",
            "EndOfWeek",
            ROW_NUMBER() OVER (ORDER BY "StartOfWeek") AS week_sequence
        FROM "WeeksNumbers"
        WHERE "EndOfWeek"::date >= CURRENT_DATE
    ),
    target_weeks AS MATERIALIZED (
        SELECT
            w."WeekNumber",
            w."StartOfWeek",
            w."EndOfWeek"
        FROM current_and_next_weeks w
        WHERE w.week_sequence BETWEEN (
            SELECT week_sequence
            FROM current_and_next_weeks
            WHERE CURRENT_DATE BETWEEN "StartOfWeek"::date AND "EndOfWeek"::date
            LIMIT 1
        ) AND (
            SELECT week_sequence + 3
            FROM current_and_next_weeks
            WHERE CURRENT_DATE BETWEEN "StartOfWeek"::date AND "EndOfWeek"::date
            LIMIT 1
        )
    ),
    expanded_availabilities AS (
        SELECT
            a."FaireeId",
            UPPER(a."Day") AS "Day",
            a."From",
            a."To",
            a."LocationId",
            a."Week"
        FROM "Availabilities" a
        WHERE a."LocationId" IS NOT NULL
        UNION ALL
        SELECT
            a."FaireeId",
            UPPER(a."Day") AS "Day",
            a."From",
            a."To",
            l."Id"        AS "LocationId",
            a."Week"
        FROM "Availabilities" a
        JOIN "Locations" l
          ON l."NeighborhoodId"::int = a."NeighborhoodId"
        WHERE a."LocationId" IS NULL
          AND a."NeighborhoodId" IS NOT NULL
    ),
    rounded_availability AS MATERIALIZED (
        SELECT
            "FaireeId",
            "Day",
            MIN(ROUND("From" * 2) / 2.0) AS "From",
            MAX(ROUND("To"   * 2) / 2.0) AS "To",
            "LocationId",
            "Week"
        FROM expanded_availabilities
        GROUP BY "FaireeId", "LocationId", "Week", "Day"
    ),
    valid_locations AS MATERIALIZED (
        SELECT "Id", "Project"
        FROM "Locations"
        WHERE "IsTest" = FALSE AND "Status" = 'ACTIVE'
    ),
    filtered_availability AS MATERIALIZED (
        SELECT a.*
        FROM rounded_availability a
        JOIN valid_locations l ON a."LocationId" = l."Id"
        JOIN target_weeks   tw ON a."Week"       = tw."WeekNumber"
        WHERE a."To" > a."From"
    ),
    time_slots AS MATERIALIZED (
        SELECT generate_series(0, 48) * 0.5 AS time_point
    ),
    availability_slots AS MATERIALIZED (
        SELECT
            a."FaireeId", a."Day", a."LocationId", a."Week", ts.time_point
        FROM filtered_availability a
        JOIN time_slots ts ON ts.time_point >= a."From" AND ts.time_point < a."To"
    ),
    rounded_blocks AS MATERIALIZED (
        SELECT
            fb."Id", fb."FaireeId",
            ROUND(fb."From" * 2) / 2.0 AS "From",
            ROUND(fb."To"   * 2) / 2.0 AS "To",
            UPPER(TRIM(TO_CHAR(fb."Date"::timestamp, 'DAY'))) AS "DayName",
            tw."WeekNumber" AS "Week"
        FROM "FaireeBlocks" fb
        JOIN target_weeks tw ON fb."Date"::date BETWEEN tw."StartOfWeek"::date AND tw."EndOfWeek"::date
    ),
    rounded_visits AS MATERIALIZED (
        SELECT
            v."Id", v."FaireeId",
            ROUND(v."StartHour" * 2) / 2.0 AS "StartHour",
            ROUND(v."EndHour"   * 2) / 2.0 AS "EndHour",
            UPPER(TRIM(TO_CHAR(v."Date"::timestamp, 'DAY'))) AS "DayName",
            tw."WeekNumber" AS "Week",
            a."LocationId"
        FROM "VisitsNew" v
        LEFT JOIN "Apartments" a ON a."Id" = v."ApartmentId"
        JOIN target_weeks tw ON v."Date"::date BETWEEN tw."StartOfWeek"::date AND tw."EndOfWeek"::date
        WHERE v."Status" NOT IN ('CANCELLED', 'TRANSFERRED')
    ),
    blocked_slots AS MATERIALIZED (
        SELECT DISTINCT
            av."FaireeId", av."Day", av."LocationId", av."Week", av.time_point
        FROM availability_slots av
        JOIN rounded_blocks b
          ON av."FaireeId"  = b."FaireeId"
         AND TRIM(av."Day") = b."DayName"
         AND av.time_point >= b."From"
         AND av.time_point <  b."To"
         AND av."Week"      = b."Week"
    ),
    visit_slots AS MATERIALIZED (
        SELECT DISTINCT
            av."FaireeId", av."Day", av."LocationId", av."Week", av.time_point
        FROM availability_slots av
        JOIN rounded_visits v
          ON av."FaireeId"  = v."FaireeId"
         AND TRIM(av."Day") = v."DayName"
         AND av.time_point >= v."StartHour"
         AND av.time_point <  v."EndHour"
         AND av."Week"      = v."Week"
    ),
    remaining_slots AS MATERIALIZED (
        SELECT
            av."FaireeId", av."Day", av."LocationId", av."Week", av.time_point
        FROM availability_slots av
        LEFT JOIN blocked_slots bs
          ON bs."FaireeId" = av."FaireeId" AND bs."Day" = av."Day"
         AND bs."LocationId" = av."LocationId" AND bs."Week" = av."Week"
         AND bs.time_point = av.time_point
        LEFT JOIN visit_slots vs
          ON vs."FaireeId" = av."FaireeId" AND vs."Day" = av."Day"
         AND vs."LocationId" = av."LocationId" AND vs."Week" = av."Week"
         AND vs.time_point = av.time_point
        WHERE bs."FaireeId" IS NULL AND vs."FaireeId" IS NULL
    ),
    slot_groups AS MATERIALIZED (
        SELECT
            "FaireeId", "Day", "LocationId", "Week", time_point,
            time_point - (
                ROW_NUMBER() OVER (
                    PARTITION BY "FaireeId", "Day", "LocationId", "Week"
                    ORDER BY time_point
                ) * 0.5
            ) AS group_id
        FROM remaining_slots
    ),
    continuous_periods AS MATERIALIZED (
        SELECT
            "FaireeId", "Day", "LocationId", "Week",
            MIN(time_point)       AS "From",
            MAX(time_point) + 0.5 AS "To"
        FROM slot_groups
        GROUP BY "FaireeId", "Day", "LocationId", "Week", group_id
    ),
    building_calendar AS MATERIALIZED (
        SELECT DISTINCT
            a."LocationId",
            l."Project",
            l."Country",
            l."State",
            l."City",
            COALESCE(n."Project", '') AS "Neighbourhood",
            a."Week"         AS "WeekNumber",
            tw."StartOfWeek" AS "WeekStartDate",
            a."Day",
            a."FaireeId"
        FROM filtered_availability a
        JOIN "Locations"  l  ON l."Id"          = a."LocationId"
        LEFT JOIN "Neighborhoods" n ON n."Id"   = l."NeighborhoodId"
        JOIN target_weeks tw ON tw."WeekNumber" = a."Week"
    ),
    booked_hours_per_loc AS MATERIALIZED (
        SELECT
            v."FaireeId", v."LocationId", v."Week" AS "WeekNumber", v."DayName" AS "Day",
            SUM(v."EndHour" - v."StartHour") AS loc_booked_hours
        FROM rounded_visits v
        GROUP BY v."FaireeId", v."LocationId", v."Week", v."DayName"
    ),
    booked_hours_per_fairee AS MATERIALIZED (
        SELECT
            "FaireeId", "WeekNumber", "Day",
            SUM(loc_booked_hours) AS fairee_total_booked_hours
        FROM booked_hours_per_loc
        GROUP BY "FaireeId", "WeekNumber", "Day"
    ),
    final AS MATERIALIZED (
        SELECT
            bc."LocationId", bc."Project", bc."Country", bc."State", bc."City",
            bc."Neighbourhood",
            bc."WeekStartDate", bc."WeekNumber", bc."Day", bc."FaireeId",
            cp."From", cp."To",
            CASE WHEN cp."From" IS NOT NULL THEN cp."To" - cp."From" ELSE 0 END AS "Duration"
        FROM building_calendar bc
        LEFT JOIN continuous_periods cp
          ON cp."LocationId" = bc."LocationId" AND cp."Week" = bc."WeekNumber"
         AND cp."Day" = bc."Day" AND cp."FaireeId" = bc."FaireeId"
    ),
    summ1 AS MATERIALIZED (
        SELECT
            f."LocationId", f."Project", f."Country", f."State", f."City",
            f."Neighbourhood",
            f."WeekStartDate", f."WeekNumber", f."Day", f."FaireeId",
            SUM(f."Duration") AS "remaining_avail",
            SUM(CASE WHEN f."Duration" > 0 AND f."Duration" <= 1 THEN 1 ELSE 0 END) AS "Slots_less_than_1hr",
            SUM(CASE WHEN f."Duration" > 1 AND f."Duration" <= 2 THEN 1 ELSE 0 END) AS "Slots_1hr_to_2hr",
            SUM(CASE WHEN f."Duration" > 2                        THEN 1 ELSE 0 END) AS "Slots_more_than_2hr"
        FROM final f
        GROUP BY
            f."LocationId", f."Project", f."Country", f."State", f."City", f."Neighbourhood",
            f."WeekStartDate", f."WeekNumber", f."Day", f."FaireeId"
    ),
    normalised_or_no AS MATERIALIZED (
        SELECT
            fa."FaireeId", fa."Week", fa."Day",
            count(distinct fa."LocationId") AS count_locations
        FROM filtered_availability fa
        GROUP BY fa."FaireeId", fa."Week", fa."Day"
        HAVING count(distinct fa."LocationId") > 1
    ),
    s2 AS MATERIALIZED (
        SELECT
            s1.*,
            (fa."To" - fa."From") AS "bookable_hours",
            CASE
                WHEN COALESCE(bf.fairee_total_booked_hours, 0) > 0
                THEN COALESCE(bl.loc_booked_hours, 0) / bf.fairee_total_booked_hours
                ELSE 0
            END AS utilisation_index,
            CASE WHEN non.count_locations > 0 THEN 1 ELSE 0 END AS "non"
        FROM summ1 s1
        LEFT JOIN booked_hours_per_loc    bl  ON bl."FaireeId" = s1."FaireeId" AND bl."LocationId" = s1."LocationId" AND bl."WeekNumber" = s1."WeekNumber" AND bl."Day" = s1."Day"
        LEFT JOIN booked_hours_per_fairee bf  ON bf."FaireeId" = s1."FaireeId" AND bf."WeekNumber" = s1."WeekNumber" AND bf."Day" = s1."Day"
        LEFT JOIN filtered_availability   fa  ON fa."LocationId" = s1."LocationId" AND fa."FaireeId" = s1."FaireeId" AND fa."Week" = s1."WeekNumber" AND fa."Day" = s1."Day"
        LEFT JOIN normalised_or_no        non ON non."FaireeId" = s1."FaireeId" AND non."Week" = s1."WeekNumber" AND non."Day" = s1."Day"
    ),
    final_summary AS MATERIALIZED (
        SELECT
            s2.*,
            CASE WHEN s2.non=1 THEN ROUND(s2.bookable_hours * s2.utilisation_index, 2) ELSE s2.bookable_hours END                  AS "effective_bookable_hours",
            CASE WHEN s2.non=1 THEN ROUND(s2.remaining_avail * s2.utilisation_index, 2) ELSE s2.remaining_avail END                AS "effective_remaining_hours",
            CASE WHEN s2.non=1 THEN ROUND(s2."Slots_less_than_1hr" * s2.utilisation_index, 2) ELSE s2."Slots_less_than_1hr" END   AS "effective_Slots_less_than_1hr",
            CASE WHEN s2.non=1 THEN ROUND(s2."Slots_1hr_to_2hr"    * s2.utilisation_index, 2) ELSE s2."Slots_1hr_to_2hr" END      AS "effective_Slots_1hr_to_2hr",
            CASE WHEN s2.non=1 THEN ROUND(s2."Slots_more_than_2hr" * s2.utilisation_index, 2) ELSE s2."Slots_more_than_2hr" END   AS "effective_Slots_more_than_2hr"
        FROM s2
    )
SELECT * FROM final_summary
"""


@contextmanager
def db():
    conn = psycopg2.connect(**_DB)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    finally:
        conn.close()


def main():
    print(f"[refresh] Starting at {datetime.utcnow().isoformat()}")
    with db() as cur:
        print("[refresh] Running availability query (may take ~30s)…")
        cur.execute(AVAILABILITY_SQL)
        df = pd.DataFrame(cur.fetchall())
        df.to_csv(DATA_DIR / "availability.csv", index=False)
        print(f"[refresh] availability: {len(df)} rows")

    (DATA_DIR / ".last_refresh").write_text(datetime.utcnow().isoformat())
    print(f"[refresh] Done at {datetime.utcnow().isoformat()}")


if __name__ == "__main__":
    main()
