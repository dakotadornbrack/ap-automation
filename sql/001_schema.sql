-- AP Automation :: core schema
-- Money is NUMERIC(14,2) everywhere. Never float.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS vendors (
    vendor_id           SERIAL PRIMARY KEY,
    name                TEXT        NOT NULL,
    normalized_name     TEXT        NOT NULL,
    payment_terms_days  INTEGER     NOT NULL DEFAULT 30,
    tax_id              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT vendors_normalized_name_key UNIQUE (normalized_name),
    CONSTRAINT vendors_terms_sane CHECK (payment_terms_days BETWEEN 0 AND 365)
);

-- Trigram index powers the fuzzy vendor-name matching in 003_rules.sql
CREATE INDEX IF NOT EXISTS vendors_normalized_name_trgm
    ON vendors USING gin (normalized_name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id      SERIAL PRIMARY KEY,
    vendor_id       INTEGER       NOT NULL REFERENCES vendors (vendor_id),
    invoice_number  TEXT          NOT NULL,
    invoice_date    DATE          NOT NULL,
    due_date        DATE          NOT NULL,
    amount          NUMERIC(14,2) NOT NULL,
    currency        CHAR(3)       NOT NULL DEFAULT 'USD',
    status          TEXT          NOT NULL DEFAULT 'open',
    gl_code         TEXT,
    source_file     TEXT          NOT NULL,
    source_row      INTEGER,
    loaded_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- Hard duplicates are rejected by the database, not just by Python.
    CONSTRAINT invoices_vendor_number_key UNIQUE (vendor_id, invoice_number),
    CONSTRAINT invoices_status_valid
        CHECK (status IN ('open', 'paid', 'void', 'on_hold')),
    CONSTRAINT invoices_due_after_invoice CHECK (due_date >= invoice_date)
);

CREATE INDEX IF NOT EXISTS invoices_due_date_idx   ON invoices (due_date);
CREATE INDEX IF NOT EXISTS invoices_status_idx     ON invoices (status);
CREATE INDEX IF NOT EXISTS invoices_vendor_idx     ON invoices (vendor_id);
-- Supports the near-duplicate self-join (same vendor, same amount, close dates)
CREATE INDEX IF NOT EXISTS invoices_near_dupe_idx
    ON invoices (vendor_id, amount, invoice_date);

CREATE TABLE IF NOT EXISTS invoice_lines (
    line_id      SERIAL PRIMARY KEY,
    invoice_id   INTEGER       NOT NULL REFERENCES invoices (invoice_id) ON DELETE CASCADE,
    line_number  INTEGER       NOT NULL,
    description  TEXT,
    quantity     NUMERIC(12,3) NOT NULL DEFAULT 1,
    unit_price   NUMERIC(14,4) NOT NULL,
    line_total   NUMERIC(14,2) NOT NULL,
    gl_code      TEXT,
    CONSTRAINT invoice_lines_invoice_line_key UNIQUE (invoice_id, line_number)
);

CREATE INDEX IF NOT EXISTS invoice_lines_invoice_idx ON invoice_lines (invoice_id);

-- Every rule in 003_rules.sql writes here. The dashboard reads only this table.
CREATE TABLE IF NOT EXISTS exceptions (
    exception_id SERIAL PRIMARY KEY,
    invoice_id   INTEGER     NOT NULL REFERENCES invoices (invoice_id) ON DELETE CASCADE,
    rule_code    TEXT        NOT NULL,
    severity     TEXT        NOT NULL,
    detail       TEXT,
    related_id   INTEGER     REFERENCES invoices (invoice_id) ON DELETE CASCADE,
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ,
    CONSTRAINT exceptions_severity_valid
        CHECK (severity IN ('info', 'warn', 'critical')),
    -- Re-running the rule engine must not pile up duplicate exceptions.
    --
    -- NULLS NOT DISTINCT is load-bearing. Five of the eight rules (OVERDUE_*,
    -- AMT_THRESHOLD, MISSING_GL, FUTURE_DATE, LINE_MISMATCH) leave related_id
    -- NULL, and under the default NULLS DISTINCT two such rows never collide --
    -- so their ON CONFLICT DO NOTHING would be a no-op and every re-run of the
    -- engine would insert the whole set again.
    CONSTRAINT exceptions_unique_finding
        UNIQUE NULLS NOT DISTINCT (invoice_id, rule_code, related_id)
);

-- Bring an existing database up to the constraint above. CREATE TABLE IF NOT
-- EXISTS leaves an already-created `exceptions` table alone, so a database
-- built before this fix keeps the permissive NULLS DISTINCT version until it is
-- replaced here. Duplicates it already accumulated are collapsed first, since
-- the new constraint cannot be added while they exist.
DO $$
BEGIN
    -- The nulls-not-distinct flag lives on the backing index, not on the
    -- constraint row, so this has to reach through conindid to read it.
    IF EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_index i ON i.indexrelid = c.conindid
        WHERE c.conname = 'exceptions_unique_finding'
          AND c.conrelid = 'exceptions'::regclass
          AND NOT i.indnullsnotdistinct
    ) THEN
        DELETE FROM exceptions e
        WHERE e.exception_id > (
            SELECT MIN(k.exception_id) FROM exceptions k
            WHERE k.invoice_id = e.invoice_id
              AND k.rule_code  = e.rule_code
              AND k.related_id IS NOT DISTINCT FROM e.related_id
        );

        ALTER TABLE exceptions DROP CONSTRAINT exceptions_unique_finding;
        ALTER TABLE exceptions ADD CONSTRAINT exceptions_unique_finding
            UNIQUE NULLS NOT DISTINCT (invoice_id, rule_code, related_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS exceptions_rule_code_idx ON exceptions (rule_code);
CREATE INDEX IF NOT EXISTS exceptions_open_idx      ON exceptions (resolved_at)
    WHERE resolved_at IS NULL;

-- Tracks which CSV files have been ingested, so re-running is a no-op.
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id        SERIAL PRIMARY KEY,
    source_file   TEXT        NOT NULL,
    file_hash     TEXT        NOT NULL,
    rows_read     INTEGER     NOT NULL DEFAULT 0,
    rows_loaded   INTEGER     NOT NULL DEFAULT 0,
    rows_rejected INTEGER     NOT NULL DEFAULT 0,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    CONSTRAINT ingest_runs_file_hash_key UNIQUE (file_hash)
);
