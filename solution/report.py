"""Report generation. All linking and aggregation happens in SQL.

Linking is recomputed from the ledger on every report run, so reports always
reflect everything ingested so far, whatever order it arrived in.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

# Step 1: link every ledger event to a lead (NULL lead_id = orphan).
#  * non-override events link by (workflow_id, phone blind index)
#  * disposition.override links through (workflow_id, call_id) to the lead of
#    that call's other (phone-linked) events, preferring call.initiated.
LINK_SQL = """
-- one sequential pass over the ledger; phone-linked events get their lead here
CREATE TEMP TABLE direct_event AS
SELECT e.seq, e.event_id, e.event_type, e.workflow_id, e.call_id, e.occurred_at_us,
       e.occurred_ist, e.occurred_ist_date, e.disposition_code,
       -- CASTs give the temp columns INTEGER affinity; without it SQLite
       -- can't use indexes when joining back to lead.lead_id (O(n*m) scans).
       CAST(CASE WHEN e.event_type IS NOT 'disposition.override' THEN l.lead_id END AS INTEGER) AS lead_id
FROM ledger_event e
LEFT JOIN lead l ON l.workflow_id = e.workflow_id AND l.phone_index = e.phone_index;

CREATE TEMP TABLE call_lead AS
SELECT workflow_id, call_id,
       CAST(COALESCE(MIN(CASE WHEN event_type = 'call.initiated' THEN lead_id END),
                     MIN(lead_id)) AS INTEGER) AS lead_id
FROM direct_event
WHERE call_id IS NOT NULL AND lead_id IS NOT NULL
GROUP BY workflow_id, call_id;
CREATE UNIQUE INDEX temp.ix_call_lead ON call_lead (workflow_id, call_id);

CREATE TEMP TABLE linked_event AS
SELECT e.seq, e.event_id, e.event_type, e.workflow_id, e.call_id, e.occurred_at_us,
       e.occurred_ist, e.occurred_ist_date, e.disposition_code,
       CAST(CASE WHEN e.event_type = 'disposition.override' THEN cl.lead_id ELSE e.lead_id END AS INTEGER) AS lead_id
FROM direct_event e
LEFT JOIN call_lead cl ON e.event_type = 'disposition.override'
                      AND cl.workflow_id = e.workflow_id AND cl.call_id = e.call_id;
CREATE INDEX temp.ix_linked_call ON linked_event (lead_id, workflow_id, call_id, event_type);
"""

# Step 2: one row per counted call (= has a linked call.initiated).
CALLS_SQL = """
CREATE TEMP TABLE call_fact AS
WITH initiated AS (
    SELECT workflow_id, call_id, lead_id,
           MIN(occurred_at_us) AS initiated_us
    FROM linked_event
    WHERE event_type = 'call.initiated' AND lead_id IS NOT NULL AND call_id IS NOT NULL
    GROUP BY workflow_id, call_id, lead_id
)
SELECT i.workflow_id, i.call_id, i.lead_id, i.initiated_us,
       (SELECT occurred_ist FROM linked_event x
         WHERE x.lead_id = i.lead_id AND x.workflow_id = i.workflow_id AND x.call_id = i.call_id
           AND x.event_type = 'call.initiated' AND x.occurred_at_us = i.initiated_us
         LIMIT 1) AS initiated_ist,
       (SELECT occurred_ist_date FROM linked_event x
         WHERE x.lead_id = i.lead_id AND x.workflow_id = i.workflow_id AND x.call_id = i.call_id
           AND x.event_type = 'call.initiated' AND x.occurred_at_us = i.initiated_us
         LIMIT 1) AS call_date,
       EXISTS (SELECT 1 FROM linked_event x
                WHERE x.lead_id = i.lead_id AND x.workflow_id = i.workflow_id
                  AND x.call_id = i.call_id AND x.event_type = 'call.connected') AS connected,
       EXISTS (SELECT 1 FROM linked_event x
                WHERE x.lead_id = i.lead_id AND x.workflow_id = i.workflow_id
                  AND x.call_id = i.call_id AND x.event_type = 'call.telephony_failed') AS telephony_failed,
       -- effective disposition: latest override, else latest set; known codes only
       (SELECT x.disposition_code FROM linked_event x
          JOIN disposition c ON c.workflow_id = x.workflow_id AND c.code = x.disposition_code
         WHERE x.lead_id = i.lead_id AND x.workflow_id = i.workflow_id AND x.call_id = i.call_id
           AND x.event_type IN ('disposition.override', 'disposition.set')
         ORDER BY x.event_type = 'disposition.override' DESC, x.occurred_at_us DESC, x.event_id DESC
         LIMIT 1) AS disposition
FROM initiated i;

CREATE TEMP TABLE call_outcome AS
SELECT f.*, COALESCE(c.is_terminal, 0) AS is_terminal, COALESCE(c.is_success, 0) AS is_success
FROM call_fact f
LEFT JOIN disposition c ON c.workflow_id = f.workflow_id AND c.code = f.disposition;
CREATE INDEX temp.ix_call_outcome_lead ON call_outcome (lead_id, initiated_us);
"""

# Step 3: one row per lead with its bucket.
LEADS_SQL = """
CREATE TEMP TABLE lead_fact AS
SELECT l.lead_id, l.workflow_id, l.external_ref, l.batch_id, l.phone_last4,
       COUNT(c.call_id)                  AS total_calls,
       COALESCE(SUM(c.connected), 0)     AS connected_calls,
       COALESCE(MAX(c.is_success), 0)    AS ever_success,
       CASE
         WHEN COUNT(c.call_id) = 0                        THEN 'yet_to_reach_out'
         WHEN MAX(c.is_terminal) = 1                      THEN 'completed'
         WHEN SUM(c.telephony_failed) = COUNT(c.call_id)  THEN 'in_progress_telephony_failed'
         ELSE 'in_progress_dialed'
       END AS bucket,
       (SELECT initiated_ist FROM call_outcome x WHERE x.lead_id = l.lead_id
         ORDER BY initiated_us DESC, call_id DESC LIMIT 1) AS last_call_at,
       (SELECT disposition FROM call_outcome x WHERE x.lead_id = l.lead_id AND disposition IS NOT NULL
         ORDER BY initiated_us DESC, call_id DESC LIMIT 1) AS current_disposition
FROM lead l
LEFT JOIN call_outcome c ON c.lead_id = l.lead_id
GROUP BY l.lead_id;
"""

BATCH_SUMMARY_SQL = """
SELECT s.workflow_id, s.batch_id,
       COUNT(f.lead_id)                                              AS total_leads,
       COALESCE(SUM(f.bucket = 'yet_to_reach_out'), 0)               AS yet_to_reach_out,
       COALESCE(SUM(f.bucket = 'in_progress_telephony_failed'), 0)   AS in_progress_telephony_failed,
       COALESCE(SUM(f.bucket = 'in_progress_dialed'), 0)             AS in_progress_dialed,
       COALESCE(SUM(f.bucket = 'completed'), 0)                      AS completed
FROM source_file s
LEFT JOIN lead_fact f ON f.workflow_id = s.workflow_id AND f.batch_id = s.batch_id
WHERE s.kind = 'leads'
GROUP BY s.workflow_id, s.batch_id
ORDER BY s.workflow_id, s.batch_id
"""

DAILY_SQL = """
SELECT workflow_id, call_date AS date,
       COUNT(*)                                             AS total_calls,
       SUM(connected)                                       AS connected_calls,
       COUNT(DISTINCT CASE WHEN is_success = 1 THEN lead_id END) AS success_leads
FROM call_outcome
GROUP BY workflow_id, call_date
ORDER BY workflow_id, call_date
"""

LEAD_REPORT_SQL = """
SELECT workflow_id, external_ref, batch_id, phone_last4, bucket, total_calls, connected_calls,
       last_call_at, current_disposition, ever_success
FROM lead_fact
ORDER BY workflow_id, external_ref, batch_id, lead_id
"""

REJECTIONS_SQL = """
SELECT workflow_id, batch_id, row_number, reason
FROM lead_rejection
ORDER BY workflow_id, batch_id, row_number
"""

QUALITY_SQL = """
SELECT
  (SELECT COALESCE(SUM(rows_total), 0)        FROM source_file WHERE kind = 'leads')  AS lead_rows_total,
  (SELECT COALESCE(SUM(rows_rejected), 0)     FROM source_file WHERE kind = 'leads')  AS lead_rows_rejected,
  (SELECT COALESCE(SUM(duplicates_merged), 0) FROM source_file WHERE kind = 'leads')  AS duplicate_lead_rows_merged,
  (SELECT COALESCE(SUM(field_warnings), 0)    FROM source_file WHERE kind = 'leads')  AS field_warnings,
  (SELECT COALESCE(SUM(lines_total), 0)       FROM source_file WHERE kind = 'events') AS events_total,
  (SELECT COALESCE(SUM(malformed), 0)         FROM source_file WHERE kind = 'events') AS malformed_events,
  (SELECT COALESCE(SUM(duplicates), 0)        FROM source_file WHERE kind = 'events') AS duplicate_events_dropped,
  (SELECT COUNT(*) FROM linked_event WHERE lead_id IS NULL)                           AS orphan_events,
  (SELECT COUNT(*) FROM linked_event e
     WHERE e.lead_id IS NOT NULL AND e.event_type LIKE 'disposition.%'
       AND NOT EXISTS (SELECT 1 FROM disposition c
                        WHERE c.workflow_id = e.workflow_id AND c.code = e.disposition_code))
                                                                                      AS unknown_disposition_events
"""


def _write_csv(path: Path, header: list[str], rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _b(v) -> str:
    return "true" if v else "false"


def build_reports(conn: sqlite3.Connection, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Temp tables live only on this connection; drop leftovers if re-run.
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -262144")  # 256 MiB page cache
    for t in ("direct_event", "call_lead", "linked_event", "call_fact", "call_outcome", "lead_fact"):
        conn.execute(f"DROP TABLE IF EXISTS temp.{t}")
    conn.executescript(LINK_SQL + CALLS_SQL + LEADS_SQL)

    _write_csv(out_dir / "batch_summary.csv",
               ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
                "in_progress_telephony_failed", "in_progress_dialed", "completed"],
               (tuple(r) for r in conn.execute(BATCH_SUMMARY_SQL)))

    _write_csv(out_dir / "daily_overview.csv",
               ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate",
                "success_leads"],
               ((r["workflow_id"], r["date"], r["total_calls"], r["connected_calls"],
                 f"{r['connected_calls'] / r['total_calls']:.4f}", r["success_leads"])
                for r in conn.execute(DAILY_SQL)))

    _write_csv(out_dir / "lead_report.csv",
               ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                "connected_calls", "last_call_at", "current_disposition", "ever_success"],
               ((r["workflow_id"], r["external_ref"], r["batch_id"], "+91******" + r["phone_last4"],
                 r["bucket"], r["total_calls"], r["connected_calls"], r["last_call_at"] or "",
                 r["current_disposition"] or "", _b(r["ever_success"]))
                for r in conn.execute(LEAD_REPORT_SQL)))

    _write_csv(out_dir / "rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"],
               (tuple(r) for r in conn.execute(REJECTIONS_SQL)))

    q = dict(conn.execute(QUALITY_SQL).fetchone())
    by_reason = dict(conn.execute(
        "SELECT reason, COUNT(*) FROM lead_rejection GROUP BY reason ORDER BY reason").fetchall())
    quality = {
        "lead_rows_total": q["lead_rows_total"],
        "lead_rows_rejected": q["lead_rows_rejected"],
        "rejections_by_reason": by_reason,
        "duplicate_lead_rows_merged": q["duplicate_lead_rows_merged"],
        "field_warnings": q["field_warnings"],
        "events_total": q["events_total"],
        "malformed_events": q["malformed_events"],
        "duplicate_events_dropped": q["duplicate_events_dropped"],
        "orphan_events": q["orphan_events"],
        "unknown_disposition_events": q["unknown_disposition_events"],
    }
    (out_dir / "data_quality.json").write_text(json.dumps(quality, indent=2) + "\n", encoding="utf-8")
    return quality
