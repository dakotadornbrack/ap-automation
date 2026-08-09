-- AP Automation :: reporting views
-- The dashboard reads these, never the base tables directly.

-- Every open invoice with its days-past-due and aging bucket.
CREATE OR REPLACE VIEW v_invoice_aging AS
SELECT
    i.invoice_id,
    i.invoice_number,
    v.vendor_id,
    v.name AS vendor_name,
    i.invoice_date,
    i.due_date,
    i.amount,
    i.currency,
    i.status,
    (CURRENT_DATE - i.due_date) AS days_past_due,
    CASE
        WHEN CURRENT_DATE <= i.due_date            THEN 'current'
        WHEN CURRENT_DATE - i.due_date <= 30       THEN '1-30'
        WHEN CURRENT_DATE - i.due_date <= 60       THEN '31-60'
        WHEN CURRENT_DATE - i.due_date <= 90       THEN '61-90'
        ELSE '90+'
    END AS aging_bucket
FROM invoices i
JOIN vendors v USING (vendor_id)
WHERE i.status = 'open';

-- Totals by bucket. Buckets with no invoices still appear, at zero.
CREATE OR REPLACE VIEW v_aging_summary AS
WITH buckets(aging_bucket, sort_order) AS (
    VALUES ('current', 1), ('1-30', 2), ('31-60', 3), ('61-90', 4), ('90+', 5)
)
SELECT
    b.aging_bucket,
    b.sort_order,
    COALESCE(COUNT(a.invoice_id), 0)  AS invoice_count,
    COALESCE(SUM(a.amount), 0)::NUMERIC(14,2) AS total_amount
FROM buckets b
LEFT JOIN v_invoice_aging a USING (aging_bucket)
GROUP BY b.aging_bucket, b.sort_order
ORDER BY b.sort_order;

-- Open balance and past-due exposure per vendor.
CREATE OR REPLACE VIEW v_vendor_exposure AS
SELECT
    v.vendor_id,
    v.name AS vendor_name,
    COUNT(*)                                        AS open_invoices,
    SUM(a.amount)::NUMERIC(14,2)                    AS open_balance,
    COUNT(*) FILTER (WHERE a.days_past_due > 0)     AS past_due_count,
    COALESCE(SUM(a.amount) FILTER (WHERE a.days_past_due > 0), 0)::NUMERIC(14,2)
                                                    AS past_due_amount,
    MAX(a.days_past_due)                            AS worst_days_past_due
FROM v_invoice_aging a
JOIN vendors v USING (vendor_id)
GROUP BY v.vendor_id, v.name;

-- Flat exception feed for the dashboard table + CSV export.
CREATE OR REPLACE VIEW v_open_exceptions AS
SELECT
    e.exception_id,
    e.rule_code,
    e.severity,
    e.detail,
    i.invoice_number,
    v.name AS vendor_name,
    i.invoice_date,
    i.due_date,
    i.amount,
    r.invoice_number AS related_invoice_number,
    e.detected_at
FROM exceptions e
JOIN invoices i  ON i.invoice_id = e.invoice_id
JOIN vendors v   ON v.vendor_id  = i.vendor_id
LEFT JOIN invoices r ON r.invoice_id = e.related_id
WHERE e.resolved_at IS NULL;

-- Single-row KPI strip for the top of the dashboard.
CREATE OR REPLACE VIEW v_kpis AS
SELECT
    (SELECT COALESCE(SUM(amount), 0)::NUMERIC(14,2) FROM v_invoice_aging)
        AS total_open_ap,
    (SELECT COUNT(*) FROM v_invoice_aging WHERE days_past_due > 0)
        AS past_due_count,
    (SELECT COALESCE(SUM(amount), 0)::NUMERIC(14,2)
       FROM v_invoice_aging WHERE days_past_due > 0)
        AS past_due_amount,
    (SELECT COUNT(*) FROM exceptions
      WHERE resolved_at IS NULL AND rule_code LIKE 'DUP_%')
        AS duplicate_flags,
    (SELECT COALESCE(SUM(i.amount), 0)::NUMERIC(14,2)
       FROM exceptions e JOIN invoices i USING (invoice_id)
      WHERE e.resolved_at IS NULL AND e.rule_code LIKE 'DUP_%')
        AS duplicate_exposure;
