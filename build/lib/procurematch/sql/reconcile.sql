-- Conciliación 3-way en SQL (DuckDB).
-- Cada regla es una consulta que emite (row_no, code). El estado final de cada línea de factura es el
-- del motivo de mayor precedencia (tabla reason_map); sin motivos, MATCH.
-- Parámetros: $as_of, $future_limit, $has_suppliers, $tol_qty, $tol_price_bp, $tol_price_abs, $tol_tax, $tol_total
-- Tablas de entrada (cargadas y validadas antes con Pydantic): inv_valid, inv_raw, inv_invalid, po_valid,
-- po_invalid, gr_valid, issues, suppliers, admitted, reason_map.

WITH
-- 1. Unicidad de factura: (proveedor, folio, línea) sin distinguir mayúsculas
inv AS (
    SELECT i.*,
           count(*) OVER (PARTITION BY lower(supplier_id), lower(invoice_id), invoice_line) AS n_key
    FROM inv_valid i
),
-- 2. Candidatos de orden: por po_line, o por SKU si la factura no trae línea
cand AS (
    SELECT i.row_no, p.row_no AS po_row_no
    FROM inv i JOIN po_valid p
      ON p.po_id = i.po_id
     AND CASE WHEN i.po_line <> '' THEN p.po_line = i.po_line ELSE p.sku = i.sku END
),
n_cand AS (
    SELECT i.row_no, count(c.po_row_no) AS n, min(c.po_row_no) AS po_row_no
    FROM inv i LEFT JOIN cand c USING (row_no)
    GROUP BY i.row_no
),
-- 3. Recepciones a la fecha de corte, netas de devoluciones
rcpt AS (
    SELECT g.po_id, g.po_line,
           sum(CASE WHEN g.movement = 'RECEIPT' THEN g.qty_received ELSE -g.qty_received END) AS qty_net,
           count(*) FILTER (WHERE g.movement = 'RECEIPT')                               AS n_receipts,
           count(*) FILTER (WHERE g.movement = 'RETURN')                                AS n_returns,
           bool_or(d.row_no IS NOT NULL)                                                AS has_dup_receipt,
           string_agg('goods_receipts.csv:fila ' || g.row_no, ';' ORDER BY g.row_no)    AS receipt_refs
    FROM gr_valid g
    LEFT JOIN (SELECT DISTINCT row_no FROM issues WHERE code = 'DUPLICATE_RECEIPT_ID') d ON d.row_no = g.row_no
    WHERE g.receipt_date <= $as_of
    GROUP BY g.po_id, g.po_line
),
-- 4. Línea de factura con su vínculo único (si existe) y sus recepciones
base AS (
    SELECT i.*, nc.n AS n_cand,
           p.row_no AS po_row_no, p.supplier_id AS po_supplier_id, p.sku AS po_sku, p.currency AS po_currency,
           p.po_date, p.qty_ordered AS po_qty_ordered, p.unit_price_minor AS po_unit_price_minor,
           p.tax_rate_bp AS po_tax_rate_bp, p.po_id AS link_po_id, p.po_line AS link_po_line,
           CASE WHEN p.row_no IS NOT NULL THEN coalesce(r.qty_net, 0) END AS qty_received_net,
           coalesce(r.n_receipts, 0) AS n_receipts, coalesce(r.n_returns, 0) AS n_returns,
           coalesce(r.has_dup_receipt, false) AS has_dup_receipt,
           CASE WHEN p.row_no IS NOT NULL THEN coalesce(r.receipt_refs, '') END AS receipt_refs,
           EXISTS (SELECT 1 FROM po_invalid x
                   WHERE x.po_id = i.po_id AND (i.po_line = '' OR x.po_line = i.po_line)) AS po_key_invalid
    FROM inv i
    JOIN n_cand nc USING (row_no)
    LEFT JOIN po_valid p ON nc.n = 1 AND p.row_no = nc.po_row_no
    LEFT JOIN rcpt r ON r.po_id = p.po_id AND r.po_line = p.po_line
),
-- 5. Reglas documentales (no dependen de cantidades)
r_doc AS (
              SELECT row_no, 'CURRENCY_NOT_ADMITTED' AS code FROM base WHERE currency NOT IN (SELECT code FROM admitted)
    UNION ALL SELECT row_no, 'FUTURE_DATED'           FROM base WHERE invoice_date > $future_limit
    UNION ALL SELECT row_no, 'UNKNOWN_SUPPLIER'       FROM base
              WHERE $has_suppliers AND lower(supplier_id) NOT IN (SELECT lower(supplier_id) FROM suppliers)
    UNION ALL SELECT row_no, 'NON_POSITIVE_QTY'       FROM base WHERE document_type = 'INVOICE' AND qty_billed <= 0
    UNION ALL SELECT row_no, 'NEGATIVE_AMOUNT'        FROM base
              WHERE document_type = 'INVOICE' AND least(unit_price_minor, tax_minor, total_minor) < 0
    UNION ALL SELECT row_no, 'CREDIT_NOTE'            FROM base WHERE document_type = 'CREDIT_NOTE'
    UNION ALL SELECT row_no, 'CREDIT_NOTE_WITHOUT_REFERENCE' FROM base
              WHERE document_type = 'CREDIT_NOTE' AND original_invoice = ''
    UNION ALL SELECT row_no, 'DUPLICATE_INVOICE'      FROM base WHERE n_key > 1
    UNION ALL SELECT row_no, 'AMBIGUOUS_PO_CANDIDATES' FROM base WHERE n_cand > 1
    UNION ALL SELECT row_no, 'PO_LINE_INVALID'        FROM base WHERE n_cand = 0 AND po_key_invalid
    UNION ALL SELECT row_no, 'PO_NOT_FOUND'           FROM base WHERE n_cand = 0 AND NOT po_key_invalid
    UNION ALL SELECT row_no, 'DUPLICATE_RECEIPT'      FROM base WHERE po_row_no IS NOT NULL AND has_dup_receipt
    UNION ALL SELECT row_no, 'NO_RECEIPT'             FROM base WHERE po_row_no IS NOT NULL AND n_receipts = 0
    UNION ALL SELECT row_no, 'SUPPLIER_MISMATCH'      FROM base
              WHERE po_row_no IS NOT NULL AND lower(supplier_id) <> lower(po_supplier_id)
    UNION ALL SELECT row_no, 'SKU_MISMATCH'           FROM base WHERE po_row_no IS NOT NULL AND sku <> po_sku
    UNION ALL SELECT row_no, 'CURRENCY_MISMATCH'      FROM base WHERE po_row_no IS NOT NULL AND currency <> po_currency
    UNION ALL SELECT row_no, 'INVOICE_BEFORE_PO'      FROM base WHERE po_row_no IS NOT NULL AND invoice_date < po_date
),
-- 6. Líneas que se comparan en cantidad e importe: factura vinculada y sin entrada bloqueada
eligible AS (
    SELECT b.*, count(*) OVER (PARTITION BY link_po_id, link_po_line) AS n_on_line
    FROM base b
    WHERE b.document_type = 'INVOICE' AND b.po_row_no IS NOT NULL
      AND b.row_no NOT IN (SELECT d.row_no FROM r_doc d JOIN reason_map m USING (code) WHERE m.status = 'BLOCKED')
),
-- 7. Facturación acumulada por línea de orden en orden cronológico; un duplicado no consume cantidad
billing AS (
    SELECT e.*,
           coalesce(sum(qty_billed) OVER (PARTITION BY link_po_id, link_po_line
                                          ORDER BY invoice_date, invoice_id, row_no
                                          ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS billed_before
    FROM eligible e
    WHERE n_key = 1
),
calc AS (
    SELECT b.*,
           greatest(qty_received_net - billed_before, 0)                       AS qty_billable,
           greatest(po_qty_ordered - billed_before, 0)                         AS ordered_left,
           currency = po_currency                                              AS same_currency,
           greatest($tol_price_abs, po_unit_price_minor * $tol_price_bp // 10000) AS tol_price,
           qty_billed * unit_price_minor                                       AS subtotal
    FROM billing b
),
money AS (
    SELECT c.*,
           qty_billed - qty_billable                                                    AS qty_diff,
           CASE WHEN same_currency THEN tol_price END                                   AS price_tolerance_minor,
           CASE WHEN same_currency THEN unit_price_minor - po_unit_price_minor END      AS price_diff_minor,
           CASE WHEN same_currency THEN (subtotal * po_tax_rate_bp + 5000) // 10000 END AS tax_expected_minor,
           CASE WHEN same_currency THEN least(qty_billed, qty_billable) * po_unit_price_minor END AS supported
    FROM calc c
),
money2 AS (
    SELECT m.*,
           tax_minor - tax_expected_minor                                   AS tax_diff_minor,
           CASE WHEN same_currency THEN total_minor - (subtotal + tax_minor) END AS total_arith_diff_minor,
           supported + (supported * po_tax_rate_bp + 5000) // 10000         AS amount_expected_minor
    FROM money m
),
-- 8. Reglas de cantidad e importe
r_qty AS (
              SELECT row_no, 'QTY_EXCEEDS_RECEIVED' AS code FROM money2
              WHERE qty_diff > $tol_qty AND n_receipts > 0
    UNION ALL SELECT row_no, 'QTY_EXCEEDS_ORDERED'       FROM money2 WHERE qty_billed - ordered_left > $tol_qty
    UNION ALL SELECT row_no, 'PRICE_OUT_OF_TOLERANCE'    FROM money2 WHERE abs(price_diff_minor) > price_tolerance_minor
    UNION ALL SELECT row_no, 'TAX_MISMATCH'              FROM money2 WHERE abs(tax_diff_minor) > $tol_tax
    UNION ALL SELECT row_no, 'TOTAL_ARITHMETIC_MISMATCH' FROM money2 WHERE abs(total_arith_diff_minor) > $tol_total
),
-- 9. Notas informativas (no cambian el estado)
notes AS (
              SELECT row_no, 'PARTIAL_RECEIPTS' AS code FROM base WHERE po_row_no IS NOT NULL AND n_receipts > 1
    UNION ALL SELECT row_no, 'RETURN_APPLIED'         FROM base WHERE po_row_no IS NOT NULL AND n_returns > 0
    UNION ALL SELECT row_no, 'PARTIAL_BILLING'        FROM money2 WHERE n_on_line > 1
    UNION ALL SELECT row_no, 'PRICE_WITHIN_TOLERANCE' FROM money2
              WHERE price_diff_minor <> 0 AND abs(price_diff_minor) <= price_tolerance_minor
),
reasons AS (SELECT * FROM r_doc UNION ALL SELECT * FROM r_qty),
verdict AS (
    SELECT r.row_no,
           arg_min(m.status, m.rank)                               AS status,
           string_agg(DISTINCT r.code, ';' ORDER BY r.code)        AS reasons
    FROM reasons r JOIN reason_map m USING (code)
    GROUP BY r.row_no
),
note_txt AS (SELECT row_no, string_agg(DISTINCT code, ';' ORDER BY code) AS notes FROM notes GROUP BY row_no),
-- 10. Filas válidas: estado por precedencia
valid_cases AS (
    SELECT b.row_no, b.invoice_id, b.invoice_line, b.supplier_id, b.po_id, b.po_line, b.sku,
           strftime(b.invoice_date, '%Y-%m-%d') AS invoice_date, b.currency, b.document_type,
           b.qty_billed, b.unit_price_minor, b.tax_minor, b.total_minor,
           coalesce(v.status, 'MATCH') AS status, coalesce(v.reasons, '') AS reasons, coalesce(n.notes, '') AS notes,
           CASE WHEN b.po_row_no IS NOT NULL THEN 'purchase_orders.csv:fila ' || b.po_row_no END AS po_ref,
           b.po_supplier_id, b.po_sku, b.po_currency, strftime(b.po_date, '%Y-%m-%d') AS po_date,
           b.po_qty_ordered, b.po_unit_price_minor, b.po_tax_rate_bp,
           coalesce(b.receipt_refs, '') AS receipt_refs, b.qty_received_net,
           m.qty_billable, m.qty_diff, m.price_diff_minor, m.price_tolerance_minor, m.tax_expected_minor,
           m.tax_diff_minor, m.total_arith_diff_minor,
           CASE WHEN m.same_currency THEN m.amount_expected_minor END AS amount_expected_minor,
           CASE WHEN m.same_currency THEN m.total_minor - m.amount_expected_minor END AS amount_diff_minor
    FROM base b
    LEFT JOIN verdict v USING (row_no)
    LEFT JOIN note_txt n USING (row_no)
    LEFT JOIN money2 m USING (row_no)
),
-- 11. Filas inválidas: se conservan con su texto original y los errores de validación como motivo
invalid_cases AS (
    SELECT r.row_no, r.invoice_id, r.invoice_line, r.supplier_id, r.po_id, r.po_line, r.sku, r.invoice_date,
           r.currency, r.document_type,
           TRY_CAST(trim(r.qty_billed) AS BIGINT)       AS qty_billed,
           TRY_CAST(trim(r.unit_price_minor) AS BIGINT) AS unit_price_minor,
           TRY_CAST(trim(r.tax_minor) AS BIGINT)        AS tax_minor,
           TRY_CAST(trim(r.total_minor) AS BIGINT)      AS total_minor,
           'BLOCKED' AS status, x.reasons, '' AS notes, '' AS receipt_refs
    FROM inv_raw r
    JOIN inv_invalid USING (row_no)
    LEFT JOIN (SELECT row_no, string_agg(DISTINCT field || ':' || code, ';' ORDER BY field || ':' || code) AS reasons
               FROM issues WHERE file = 'invoice_lines.csv' GROUP BY row_no) x USING (row_no)
)
SELECT printf('L%05d', c.row_no) AS case_id, 'invoice_lines.csv:fila ' || c.row_no AS invoice_ref, c.*,
       s.label AS status_label
FROM (SELECT * FROM valid_cases UNION ALL BY NAME SELECT * FROM invalid_cases) c
JOIN status_labels s ON s.status = c.status
ORDER BY c.row_no
