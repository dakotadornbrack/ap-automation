-- AP Automation :: rule engine
-- Every statement is INSERT ... ON CONFLICT DO NOTHING, so the whole file is
-- safe to re-run. Each rule writes one row per finding into `exceptions`.
--
-- Rule codes:
--   DUP_EXACT      same vendor, same invoice number ignoring formatting
--   DUP_NEAR       same vendor, same amount, invoice dates within 5 days
--   DUP_FUZZY_VEND same amount + number across near-identical vendor names
--   OVERDUE_30/60/90  open past due date by that many days
--   AMT_THRESHOLD  above the approval limit
--   MISSING_GL     no GL code on an open invoice
--   FUTURE_DATE    invoice dated in the future
--   LINE_MISMATCH  line totals do not sum to the header amount

-- Strips punctuation and leading zeros so INV-0042, inv 42 and INV42 collide.
-- Must stay behaviourally identical to normalize_invoice_number() in
-- src/ap/clean.py; tests/test_parity.py asserts the two agree.
CREATE OR REPLACE FUNCTION normalize_invoice_number(txt TEXT)
RETURNS TEXT AS $$
    SELECT NULLIF(
        regexp_replace(
            regexp_replace(lower(COALESCE(txt, '')), '[^a-z0-9]', '', 'g'),
            '(^|[^0-9])0+([0-9])', '\1\2', 'g'
        ), '');
$$ LANGUAGE sql IMMUTABLE;


-- ---------------------------------------------------------------- DUP_EXACT
-- The UNIQUE (vendor_id, invoice_number) constraint blocks byte-identical
-- duplicates at load time. This catches the ones that slip past it because the
-- formatting differs.
INSERT INTO exceptions (invoice_id, rule_code, severity, detail, related_id)
SELECT
    dupe.invoice_id,
    'DUP_EXACT',
    'critical',
    format('Same vendor and invoice number as %s (loaded %s)',
           orig.invoice_number, orig.loaded_at::date),
    orig.invoice_id
FROM invoices dupe
JOIN invoices orig
  ON  orig.vendor_id = dupe.vendor_id
  AND normalize_invoice_number(orig.invoice_number)
      = normalize_invoice_number(dupe.invoice_number)
  AND orig.invoice_id < dupe.invoice_id   -- flag the later arrival, cite the first
WHERE dupe.status <> 'void'
ON CONFLICT ON CONSTRAINT exceptions_unique_finding DO NOTHING;


-- ----------------------------------------------------------------- DUP_NEAR
-- The classic AP double-pay: vendor re-sends an invoice under a new number.
-- Same payee, same amount, a few days apart.
INSERT INTO exceptions (invoice_id, rule_code, severity, detail, related_id)
SELECT
    dupe.invoice_id,
    'DUP_NEAR',
    'critical',
    format('Same vendor and amount (%s) as %s, %s day(s) apart',
           to_char(dupe.amount, 'FM999,999,990.00'),
           orig.invoice_number,
           abs(dupe.invoice_date - orig.invoice_date)),
    orig.invoice_id
FROM invoices dupe
JOIN invoices orig
  ON  orig.vendor_id = dupe.vendor_id
  AND orig.amount    = dupe.amount
  AND orig.invoice_date BETWEEN dupe.invoice_date - INTERVAL '5 days'
                            AND dupe.invoice_date + INTERVAL '5 days'
  AND orig.invoice_id < dupe.invoice_id
WHERE dupe.status <> 'void'
  -- Don't double-report something DUP_EXACT already caught.
  AND normalize_invoice_number(orig.invoice_number)
      IS DISTINCT FROM normalize_invoice_number(dupe.invoice_number)
ON CONFLICT ON CONSTRAINT exceptions_unique_finding DO NOTHING;


-- ------------------------------------------------------------ DUP_FUZZY_VEND
-- Same invoice hitting the ledger twice under "ACME Corp" and "Acme Corporation",
-- which cleaning did not collapse into one vendor record.
INSERT INTO exceptions (invoice_id, rule_code, severity, detail, related_id)
SELECT
    dupe.invoice_id,
    'DUP_FUZZY_VEND',
    'warn',
    -- format() accepts only %s/%I/%L -- there is no %f and no precision field,
    -- so the rounding has to happen in SQL before the value reaches format().
    format('Amount %s also billed by similar vendor "%s" (similarity %s)',
           to_char(dupe.amount, 'FM999,999,990.00'),
           ov.name,
           round(similarity(dv.normalized_name, ov.normalized_name)::numeric, 2)),
    orig.invoice_id
FROM invoices dupe
JOIN vendors  dv ON dv.vendor_id = dupe.vendor_id
JOIN vendors  ov ON ov.vendor_id <> dv.vendor_id
                AND similarity(dv.normalized_name, ov.normalized_name) >= 0.55
JOIN invoices orig
  ON  orig.vendor_id = ov.vendor_id
  AND orig.amount    = dupe.amount
  AND orig.invoice_date BETWEEN dupe.invoice_date - INTERVAL '15 days'
                            AND dupe.invoice_date + INTERVAL '15 days'
  AND orig.invoice_id < dupe.invoice_id
WHERE dupe.status <> 'void'
ON CONFLICT ON CONSTRAINT exceptions_unique_finding DO NOTHING;


-- ------------------------------------------------------------------ OVERDUE
-- One row per invoice at its worst tier, not three rows as it ages.
--
-- The CASE below picks a single tier per invoice, so one run can only ever
-- write one row. Across runs is the problem: an invoice flagged OVERDUE_30 in
-- April is OVERDUE_60 in May, and those are different rule_codes, so the
-- unique constraint sees a different finding and ON CONFLICT lets the second
-- one through. The invoice ends up carrying two open findings, then three.
--
-- So retire the stale tier first. The queue is a list of invoices needing
-- action now, not an audit trail of how each one aged -- an AP clerk working
-- the queue should meet every invoice exactly once. Only unresolved rows are
-- touched: a finding somebody has already signed off on is history, and
-- rewriting history under a reviewer is worse than a duplicate.
DELETE FROM exceptions e
USING v_invoice_aging a
WHERE e.invoice_id = a.invoice_id
  AND e.resolved_at IS NULL
  AND e.rule_code IN ('OVERDUE_30', 'OVERDUE_60', 'OVERDUE_90')
  AND e.rule_code <> CASE
        WHEN a.days_past_due > 90 THEN 'OVERDUE_90'
        WHEN a.days_past_due > 60 THEN 'OVERDUE_60'
        ELSE 'OVERDUE_30'
      END;

INSERT INTO exceptions (invoice_id, rule_code, severity, detail)
SELECT
    invoice_id,
    CASE WHEN days_past_due > 90 THEN 'OVERDUE_90'
         WHEN days_past_due > 60 THEN 'OVERDUE_60'
         ELSE 'OVERDUE_30'
    END,
    CASE WHEN days_past_due > 90 THEN 'critical' ELSE 'warn' END,
    format('%s days past due (due %s)', days_past_due, due_date)
FROM v_invoice_aging
WHERE days_past_due > 30
ON CONFLICT ON CONSTRAINT exceptions_unique_finding DO NOTHING;


-- ------------------------------------------------------------ AMT_THRESHOLD
INSERT INTO exceptions (invoice_id, rule_code, severity, detail)
SELECT
    invoice_id,
    'AMT_THRESHOLD',
    'warn',
    format('Amount %s exceeds the %s approval threshold',
           to_char(amount, 'FM999,999,990.00'), '10,000.00')
FROM invoices
WHERE amount > 10000 AND status = 'open'
ON CONFLICT ON CONSTRAINT exceptions_unique_finding DO NOTHING;


-- --------------------------------------------------------------- MISSING_GL
INSERT INTO exceptions (invoice_id, rule_code, severity, detail)
SELECT invoice_id, 'MISSING_GL', 'info', 'No GL code assigned'
FROM invoices
WHERE status = 'open' AND (gl_code IS NULL OR btrim(gl_code) = '')
ON CONFLICT ON CONSTRAINT exceptions_unique_finding DO NOTHING;


-- -------------------------------------------------------------- FUTURE_DATE
INSERT INTO exceptions (invoice_id, rule_code, severity, detail)
SELECT
    invoice_id, 'FUTURE_DATE', 'warn',
    format('Invoice dated %s, which is in the future', invoice_date)
FROM invoices
WHERE invoice_date > CURRENT_DATE
ON CONFLICT ON CONSTRAINT exceptions_unique_finding DO NOTHING;


-- ------------------------------------------------------------ LINE_MISMATCH
-- Tolerance of one cent absorbs legitimate rounding on unit-price math.
INSERT INTO exceptions (invoice_id, rule_code, severity, detail)
SELECT
    i.invoice_id,
    'LINE_MISMATCH',
    'critical',
    format('Lines total %s but header says %s',
           to_char(SUM(l.line_total), 'FM999,999,990.00'),
           to_char(i.amount, 'FM999,999,990.00'))
FROM invoices i
JOIN invoice_lines l USING (invoice_id)
GROUP BY i.invoice_id, i.amount
HAVING abs(SUM(l.line_total) - i.amount) > 0.01
ON CONFLICT ON CONSTRAINT exceptions_unique_finding DO NOTHING;
