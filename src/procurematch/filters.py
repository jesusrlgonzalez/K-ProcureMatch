"""Filtros de la vista del analista. Módulo sin dependencias: lo comparten API, motor y cliente HTTP."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Filters:
    suppliers: list[str] = field(default_factory=list)
    currencies: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)          # al menos uno de los motivos
    review_states: list[str] = field(default_factory=list)    # PENDIENTE / RESUELTO
    date_from: date | None = None
    date_to: date | None = None
    include_undated: bool = True   # facturas con fecha inválida no desaparecen al filtrar por periodo

    def as_dict(self) -> dict:
        d = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in self.__dict__.items()
             if v and k != "include_undated"}
        if (self.date_from or self.date_to) and not self.include_undated:
            d["include_undated"] = False
        return d

    def query_params(self) -> list[tuple[str, str]]:
        """Parámetros de consulta de la API (los nombres coinciden con los de `GET /runs/{id}/...`)."""
        p: list[tuple[str, str]] = []
        for key, values in (("supplier", self.suppliers), ("currency", self.currencies), ("status", self.statuses),
                            ("reason", self.reasons), ("review_state", self.review_states)):
            p += [(key, v) for v in values]
        if self.date_from:
            p.append(("date_from", self.date_from.isoformat()))
        if self.date_to:
            p.append(("date_to", self.date_to.isoformat()))
        if not self.include_undated:
            p.append(("include_undated", "false"))
        return p
