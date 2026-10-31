# Catálogo de reglas (R1.0.0)

Cada línea de factura recibe **un único estado**. Si se activan varios motivos, gana el de mayor precedencia: `BLOCKED` > `REVIEW` > `UNMATCHED` > `EXCEPTION` > `MATCH`.

Las reglas están implementadas en SQL en [`src/procurematch/sql/reconcile.sql`](../src/procurematch/sql/reconcile.sql): cada motivo es una consulta que emite `(fila, código)`. Las tolerancias viven en [`rules/rules_v1.yaml`](../rules/rules_v1.yaml). El expediente registra la versión y el SHA-256 de ese archivo. Cambiar una tolerancia exige publicar una nueva versión de reglas.

## Estados

| Estado | Etiqueta | Significado |
|---|---|---|
| `MATCH` | Conciliado | Vínculo 3-way único y todas las diferencias dentro de tolerancia |
| `EXCEPTION` | Discrepante | Vínculo único con una discrepancia documental o monetaria fuera de tolerancia |
| `UNMATCHED` | Sin vínculo | No existe orden o recepción que respalde la factura |
| `REVIEW` | Revisión | Duplicado, ambigüedad o documento que exige criterio humano; no se vincula automáticamente |
| `BLOCKED` | Bloqueado | Entrada inválida; no se intenta conciliar |

## Motivos

| Código | Estado que fuerza | Descripción |
|---|---|---|
| `PO_LINE_INVALID` | `BLOCKED` | La línea de orden referida existe pero es inválida |
| `CURRENCY_NOT_ADMITTED` | `BLOCKED` | Moneda no admitida por las reglas |
| `FUTURE_DATED` | `BLOCKED` | Fecha de factura posterior a la fecha de corte |
| `UNKNOWN_SUPPLIER` | `BLOCKED` | Proveedor no existe en el maestro |
| `NON_POSITIVE_QTY` | `BLOCKED` | Factura con cantidad cero o negativa |
| `NEGATIVE_AMOUNT` | `BLOCKED` | Factura con importe negativo |
| `DUPLICATE_INVOICE` | `REVIEW` | Misma factura (proveedor, folio, línea) aparece más de una vez |
| `AMBIGUOUS_PO_CANDIDATES` | `REVIEW` | Más de una línea de orden candidata |
| `DUPLICATE_RECEIPT` | `REVIEW` | La recepción vinculada tiene folio duplicado |
| `CREDIT_NOTE` | `REVIEW` | Nota de crédito: requiere revisión humana |
| `CREDIT_NOTE_WITHOUT_REFERENCE` | `REVIEW` | Nota de crédito sin factura original |
| `PO_NOT_FOUND` | `UNMATCHED` | No existe orden de compra para la referencia |
| `NO_RECEIPT` | `UNMATCHED` | La orden no tiene recepción que respalde la factura |
| `SUPPLIER_MISMATCH` | `EXCEPTION` | El proveedor de la factura no coincide con el de la orden |
| `SKU_MISMATCH` | `EXCEPTION` | El SKU de la factura no coincide con el de la orden |
| `CURRENCY_MISMATCH` | `EXCEPTION` | La moneda de la factura no coincide con la de la orden |
| `INVOICE_BEFORE_PO` | `EXCEPTION` | Factura fechada antes que la orden |
| `PRICE_OUT_OF_TOLERANCE` | `EXCEPTION` | Precio unitario fuera de tolerancia |
| `QTY_EXCEEDS_RECEIVED` | `EXCEPTION` | Cantidad facturada supera lo recibido no facturado |
| `QTY_EXCEEDS_ORDERED` | `EXCEPTION` | Cantidad facturada supera lo ordenado pendiente |
| `TAX_MISMATCH` | `EXCEPTION` | Impuesto distinto al calculado con la tasa de la orden |
| `TOTAL_ARITHMETIC_MISMATCH` | `EXCEPTION` | Total distinto de subtotal + impuesto |
| `<campo>:<código>` | `BLOCKED` | Error de formato en la fila: `MISSING_KEY`, `INVALID_ID_FORMAT`, `INVALID_NUMBER`, `NEGATIVE_NOT_ALLOWED`, `INVALID_DATE`, `INVALID_CURRENCY`, `INVALID_DOCUMENT_TYPE` |

## Notas informativas (no cambian el estado)

| Código | Descripción |
|---|---|
| `PARTIAL_BILLING` | La orden se factura en varias facturas |
| `PARTIAL_RECEIPTS` | Cantidad cubierta por varias recepciones |
| `PRICE_WITHIN_TOLERANCE` | Diferencia de precio dentro de tolerancia |
| `RETURN_APPLIED` | Se descontaron devoluciones de la recepción |

## Cálculos

- **Tolerancia de precio:** `max(unit_price_abs_minor, precio_OC × unit_price_bp / 10 000)`.
- **Impuesto esperado:** `cantidad × precio_factura × tasa_OC`, redondeado al centavo (half-up).
- **Cantidad facturable:** recibido neto (recepciones − devoluciones, con fecha ≤ corte) − lo ya facturado antes en esa línea de orden, en orden cronológico. Un duplicado no consume cantidad.
- **Importe respaldado:** `min(cantidad facturada, facturable) × precio_OC`, más su impuesto.
- **Diferencia monetaria:** total facturado − importe respaldado. Se informa siempre por moneda, sin sumar monedas distintas.
