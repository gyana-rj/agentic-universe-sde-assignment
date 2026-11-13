-- Interaction ledger schema (SQLite).
--
-- Design in one paragraph: raw inputs are recorded once per *file*
-- (source_file), which is what makes re-ingest a no-op. Events go into an
-- append-only ledger (ledger_event) that is never updated or deleted, so
-- event->lead linking is NOT stored: it is derived at report time. An event
-- that was an orphan after drop_1 therefore links automatically once its lead
-- arrives in drop_2. Leads are a merged projection of accepted CSV rows; the
-- per-row history is kept in lead_row. No PII column holds plaintext.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Disposition catalogue, refreshed from dispositions.csv on every run.
CREATE TABLE IF NOT EXISTS disposition (
    workflow_id TEXT    NOT NULL,
    code        TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    is_terminal INTEGER NOT NULL CHECK (is_terminal IN (0, 1)),
    is_success  INTEGER NOT NULL CHECK (is_success  IN (0, 1)),
    PRIMARY KEY (workflow_id, code)
);

-- One row per ingested input file, with its counters. A file is ingested in a
-- single transaction together with this row, so it is either fully in or not
-- at all (crash-safe), and a second attempt is detected and skipped.
CREATE TABLE IF NOT EXISTS source_file (
    file_id           INTEGER PRIMARY KEY,
    kind              TEXT    NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id       TEXT,                 -- leads only
    batch_id          TEXT,                 -- leads only: file stem
    path              TEXT    NOT NULL,     -- informational
    sha256            TEXT    NOT NULL,
    ingested_at       TEXT    NOT NULL,
    -- lead-file counters
    rows_total        INTEGER NOT NULL DEFAULT 0,
    rows_rejected     INTEGER NOT NULL DEFAULT 0,
    duplicates_merged INTEGER NOT NULL DEFAULT 0,
    field_warnings    INTEGER NOT NULL DEFAULT 0,
    -- event-file counters
    lines_total       INTEGER NOT NULL DEFAULT 0,
    malformed         INTEGER NOT NULL DEFAULT 0,
    duplicates        INTEGER NOT NULL DEFAULT 0,
    CHECK ((kind = 'leads') = (workflow_id IS NOT NULL AND batch_id IS NOT NULL))
);
-- A batch is immutable: (workflow, batch) can be ingested once.
CREATE UNIQUE INDEX IF NOT EXISTS ux_source_file_batch
    ON source_file (workflow_id, batch_id) WHERE kind = 'leads';
-- An event file is identified by its content.
CREATE UNIQUE INDEX IF NOT EXISTS ux_source_file_events
    ON source_file (sha256) WHERE kind = 'events';

-- Merged lead projection. Identity = (workflow_id, phone).
CREATE TABLE IF NOT EXISTS lead (
    lead_id         INTEGER PRIMARY KEY,
    workflow_id     TEXT    NOT NULL,
    phone_index     TEXT    NOT NULL,   -- HMAC-SHA256 of canonical phone (blind index)
    phone_enc       BLOB    NOT NULL,   -- AES-GCM(canonical phone)
    phone_last4     TEXT    NOT NULL,   -- for masking only; not identifying on its own
    batch_id        TEXT    NOT NULL,   -- batch of the first valid row, never changes
    external_ref    TEXT    NOT NULL,   -- value of the schema's reference field
    attrs_json      TEXT    NOT NULL,   -- merged non-PII fields (null = missing)
    pii_enc         BLOB    NOT NULL,   -- AES-GCM(JSON of merged PII fields except phone)
    created_file_id INTEGER NOT NULL REFERENCES source_file (file_id),
    updated_file_id INTEGER NOT NULL REFERENCES source_file (file_id),
    UNIQUE (workflow_id, phone_index)
);

-- Audit trail: which accepted CSV row created or was merged into which lead.
CREATE TABLE IF NOT EXISTS lead_row (
    file_id    INTEGER NOT NULL REFERENCES source_file (file_id),
    row_number INTEGER NOT NULL,
    lead_id    INTEGER NOT NULL REFERENCES lead (lead_id),
    action     TEXT    NOT NULL CHECK (action IN ('created', 'merged')),
    warnings   TEXT    NOT NULL DEFAULT '',  -- comma-separated field names
    PRIMARY KEY (file_id, row_number)
);

-- Rejected lead rows. No PII by contract.
CREATE TABLE IF NOT EXISTS lead_rejection (
    workflow_id TEXT    NOT NULL,
    batch_id    TEXT    NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT    NOT NULL,
    file_id     INTEGER NOT NULL REFERENCES source_file (file_id),
    PRIMARY KEY (workflow_id, batch_id, row_number)
);

-- The interaction ledger: every well-formed, first-seen event, append-only.
-- Split in two so the columns every report scans stay narrow:
--   ledger_event        typed, normalised facts (hot)
--   ledger_event_detail the rest of the original event (cold, for audit)
CREATE TABLE IF NOT EXISTS ledger_event (
    seq               INTEGER PRIMARY KEY,         -- ingest order
    event_id          TEXT    NOT NULL UNIQUE,     -- dedupe key across all drops
    file_id           INTEGER NOT NULL REFERENCES source_file (file_id),
    line_no           INTEGER NOT NULL,
    event_type        TEXT,
    workflow_id       TEXT,
    call_id           TEXT,
    occurred_at_us    INTEGER NOT NULL,            -- UTC epoch microseconds
    occurred_ist      TEXT    NOT NULL,            -- '2026-10-14T17:55:24+05:30'
    occurred_ist_date TEXT    NOT NULL,            -- '2026-10-14'
    phone_index       TEXT,                        -- HMAC of normalised phone, NULL if absent/invalid
    disposition_code  TEXT                         -- payload.disposition_code, disposition.* only
);

CREATE TABLE IF NOT EXISTS ledger_event_detail (
    seq             INTEGER PRIMARY KEY REFERENCES ledger_event (seq),
    occurred_at_raw TEXT NOT NULL,                 -- timestamp exactly as received
    phone_enc       BLOB,                          -- AES-GCM of the raw phone string as received
    payload_json    TEXT NOT NULL,
    extra_json      TEXT                           -- any unexpected top-level keys
);

CREATE TRIGGER IF NOT EXISTS ledger_event_no_update
BEFORE UPDATE ON ledger_event
BEGIN
    SELECT RAISE(ABORT, 'ledger_event is append-only');
END;

CREATE TRIGGER IF NOT EXISTS ledger_event_no_delete
BEFORE DELETE ON ledger_event
BEGIN
    SELECT RAISE(ABORT, 'ledger_event is append-only');
END;

CREATE TRIGGER IF NOT EXISTS ledger_event_detail_no_update
BEFORE UPDATE ON ledger_event_detail
BEGIN
    SELECT RAISE(ABORT, 'ledger_event_detail is append-only');
END;

CREATE TRIGGER IF NOT EXISTS ledger_event_detail_no_delete
BEFORE DELETE ON ledger_event_detail
BEGIN
    SELECT RAISE(ABORT, 'ledger_event_detail is append-only');
END;
