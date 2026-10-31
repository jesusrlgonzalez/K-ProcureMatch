# Contrato de datos

Tres fuentes obligatorias y un maestro opcional, en CSV UTF-8 con encabezado. Los importes van en **unidades menores enteras** (centavos): no se usan decimales ni flotantes. Las fechas van en ISO `AAAA-MM-DD`.

Toda fila se valida. Una fila inválida **no se elimina**: queda en `incidencias_validacion.csv` con archivo, fila, campo y código, y su factura se clasifica `BLOCKED`.

## `purchase_orders.csv` — líneas de orden de compra
| Campo | Tipo | Regla |
|---|---|---|
| `po_id`, `po_line` | texto | obligatorios; juntos identifican la línea. Si se repiten, la línea es ambigua |
| `supplier_id` | texto | obligatorio |
| `sku` | texto | obligatorio |
| `qty_ordered` | entero ≥ 0 | |
| `unit_price_minor` | entero ≥ 0 | precio unitario en centavos |
| `tax_rate_bp` | entero ≥ 0 | tasa de impuesto en puntos base (1600 = 16 %) |
| `currency` | ISO 4217 | tres letras mayúsculas |
| `po_date` | fecha | |

## `goods_receipts.csv` — recepciones y devoluciones
| Campo | Tipo | Regla |
|---|---|---|
| `receipt_id` | texto | obligatorio; si se repite, se marca `DUPLICATE_RECEIPT_ID` |
| `po_id`, `po_line`, `sku` | texto | vínculo con la orden |
| `qty_received` | entero ≥ 0 | |
| `receipt_date` | fecha | solo cuentan las recepciones con fecha ≤ fecha de corte |
| `movement` | `RECEIPT` \| `RETURN` | opcional (por defecto `RECEIPT`); una devolución resta de lo recibido |

## `invoice_lines.csv` — líneas de factura y notas de crédito
| Campo | Tipo | Regla |
|---|---|---|
| `invoice_id`, `invoice_line` | texto | obligatorios |
| `supplier_id` | texto | obligatorio; con `invoice_id` e `invoice_line` forma la clave de unicidad |
| `po_id` | texto | obligatorio |
| `po_line` | texto | opcional; si falta, el vínculo se intenta por `po_id` + `sku` |
| `sku` | texto | obligatorio |
| `invoice_date` | fecha | |
| `qty_billed` | entero | en una factura debe ser > 0 |
| `unit_price_minor`, `tax_minor`, `total_minor` | entero | en una factura deben ser ≥ 0 |
| `currency` | ISO 4217 | debe estar admitida por las reglas |
| `document_type` | `INVOICE` \| `CREDIT_NOTE` | |
| `original_invoice` | texto | referencia de una nota de crédito |

## `suppliers.csv` — maestro de proveedores (opcional)
`supplier_id`, `legal_name`, `alias`. Si se proporciona, una factura de un proveedor que no esté en el maestro se bloquea con `UNKNOWN_SUPPLIER`.

## Datos de demostración
`data/sample/` contiene un conjunto **sintético** y determinista, generado con `python run.py fixture`: 749 líneas de factura en MXN y USD con 27 escenarios. `expected_labels.csv` guarda el estado correcto de cada línea. El motor nunca lee ese archivo: solo se usa para medir la exactitud.
