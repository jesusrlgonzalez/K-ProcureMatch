"""Contratos de datos. Los CSV se leen como texto y se validan aquí: nada se descarta en silencio."""
from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
CUR_RE = re.compile(r"^[A-Z]{3}$")


def _required_id(v: str) -> str:
    v = (v or "").strip()
    if not v:
        raise ValueError("MISSING_KEY")
    if not ID_RE.match(v):
        raise ValueError("INVALID_ID_FORMAT")
    return v


def _int(v: str, *, allow_negative: bool = False) -> int:
    s = (v or "").strip()
    if not re.fullmatch(r"-?\d+", s):
        raise ValueError("INVALID_NUMBER")
    n = int(s)
    if n < 0 and not allow_negative:
        raise ValueError("NEGATIVE_NOT_ALLOWED")
    return n


def _date(v: str) -> date:
    try:
        return date.fromisoformat((v or "").strip())
    except ValueError:
        raise ValueError("INVALID_DATE") from None


def _currency(v: str) -> str:
    v = (v or "").strip()
    if not CUR_RE.match(v):
        raise ValueError("INVALID_CURRENCY")
    return v


class _Row(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    row_no: int = Field(description="Fila de datos (1 = primera fila tras el encabezado)")


class Supplier(_Row):
    supplier_id: str
    legal_name: str = ""
    alias: str = ""

    _id = field_validator("supplier_id", mode="before")(_required_id)


class PurchaseOrderLine(_Row):
    po_id: str
    po_line: str
    supplier_id: str
    sku: str
    qty_ordered: int
    unit_price_minor: int
    tax_rate_bp: int
    currency: str
    po_date: date

    _ids = field_validator("po_id", "po_line", "supplier_id", "sku", mode="before")(_required_id)
    _cur = field_validator("currency", mode="before")(_currency)
    _dt = field_validator("po_date", mode="before")(_date)

    @field_validator("qty_ordered", "unit_price_minor", "tax_rate_bp", mode="before")
    @classmethod
    def _n(cls, v):
        return _int(v)


class GoodsReceipt(_Row):
    receipt_id: str
    po_id: str
    po_line: str
    sku: str
    qty_received: int
    receipt_date: date
    movement: Literal["RECEIPT", "RETURN"] = "RECEIPT"

    _ids = field_validator("receipt_id", "po_id", "po_line", "sku", mode="before")(_required_id)
    _dt = field_validator("receipt_date", mode="before")(_date)

    @field_validator("qty_received", mode="before")
    @classmethod
    def _n(cls, v):
        return _int(v)

    @field_validator("movement", mode="before")
    @classmethod
    def _m(cls, v):
        v = (v or "RECEIPT").strip().upper()
        if v not in ("RECEIPT", "RETURN"):
            raise ValueError("INVALID_MOVEMENT")
        return v


class InvoiceLine(_Row):
    invoice_id: str
    invoice_line: str
    supplier_id: str
    po_id: str
    po_line: str = ""
    sku: str
    invoice_date: date
    qty_billed: int
    unit_price_minor: int
    tax_minor: int
    total_minor: int
    currency: str
    document_type: Literal["INVOICE", "CREDIT_NOTE"]
    original_invoice: str = ""

    _ids = field_validator("invoice_id", "invoice_line", "supplier_id", "po_id", "sku", mode="before")(_required_id)
    _cur = field_validator("currency", mode="before")(_currency)
    _dt = field_validator("invoice_date", mode="before")(_date)

    @field_validator("po_line", "original_invoice", mode="before")
    @classmethod
    def _opt(cls, v):
        return (v or "").strip()

    @field_validator("qty_billed", "unit_price_minor", "tax_minor", "total_minor", mode="before")
    @classmethod
    def _n(cls, v):
        return _int(v, allow_negative=True)  # el signo lo evalúa la regla según el tipo de documento

    @field_validator("document_type", mode="before")
    @classmethod
    def _doc(cls, v):
        v = (v or "").strip().upper()
        if v not in ("INVOICE", "CREDIT_NOTE"):
            raise ValueError("INVALID_DOCUMENT_TYPE")
        return v


class Status(str, Enum):
    MATCH = "MATCH"            # vínculo 3-way único y diferencias dentro de tolerancia
    EXCEPTION = "EXCEPTION"    # vínculo único pero discrepancia fuera de tolerancia
    REVIEW = "REVIEW"          # duplicado, ambigüedad o documento que exige criterio humano
    UNMATCHED = "UNMATCHED"    # sin orden o sin recepción que la respalde
    BLOCKED = "BLOCKED"        # entrada inválida: no se intenta conciliar


STATUS_LABEL = {
    Status.MATCH: "Conciliado",
    Status.EXCEPTION: "Discrepante",
    Status.REVIEW: "Revisión",
    Status.UNMATCHED: "Sin vínculo",
    Status.BLOCKED: "Bloqueado",
}


class Decision(str, Enum):
    CONFIRM = "CONFIRMAR"                    # el analista confirma la clasificación
    ACCEPT_WITH_JUSTIFICATION = "ACEPTAR_CON_JUSTIFICACION"
    REJECT = "RECHAZAR"
    REQUEST_CREDIT_NOTE = "SOLICITAR_NOTA_CREDITO"
    ESCALATE = "REMITIR"                     # se remite a compras / tesorería


class Resolution(BaseModel):
    """Decisión humana. No ejecuta pagos, asientos ni órdenes en ERP."""
    model_config = ConfigDict(extra="forbid")
    decision: Decision
    analyst: str = Field(min_length=2, max_length=80)
    comment: str = Field(default="", max_length=2000)

    @field_validator("analyst")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("analyst requerido")
        return v
