# ProcureMatch · Conciliación inteligente de compras industriales

**Reducir errores administrativos y discrepancias en operaciones de abastecimiento mediante conciliación documentada de órdenes de compra, recepciones y facturas.**

## Qué resuelve
Verificar registros de proveedores, órdenes, recepciones y facturas; detectar discrepancias de cantidad, precio o identidad; y clasificar los casos para revisión responsable.

Motor de conciliación 3-way (orden de compra · recepción · factura) basado en reglas explícitas con tolerancias: valida claves, cantidades, precios e impuestos, y clasifica cada línea como candidata a match, revisión humana o bloqueo, con el motivo registrado.

## Hacia dónde va
Convertirse en una capa inteligente de conciliación empresarial capaz de integrarse con sistemas ERP y facilitar controles financieros y operativos auditables.

**Objetivo:** Desarrollar y validar un motor de conciliación 3-way que compare documentos transaccionales, identifique excepciones y mida exactitud, diferencias monetarias y tiempo de revisión.

**Valor que persigue:** Reducir conciliaciones incorrectas, retrabajos administrativos y costos de revisión en cuentas por pagar.

## Resultados de la demo
Sobre un conjunto de datos sintéticos diseñado con errores y casos difíciles: **784 líneas conciliadas: 716 candidatas a match, 8 a revisión, 60 bloqueadas**.

## Pruébalo en 2 minutos
```bash
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py test     # 24 tests automatizados
python run.py demo     # demo de punta a punta → demo_output/
```

## Diseñado para decisiones responsables
- **Siempre con una persona al mando:** no paga, no contabiliza y no aprueba facturas; solo clasifica para revisión responsable.
- **Trazable:** cada registro conserva su motivo de aceptación, revisión o cuarentena.
- **Probado:** 24 tests automatizados en Linux y Windows en cada cambio.

## Estado
Prototipo funcional demostrado con datos sintéticos. El siguiente paso es un **piloto con datos reales** de su organización, bajo acuerdo de uso y confidencialidad.

**¿Le interesa un piloto?** Contacte a **Kriter Systems**.

---
I19 · Kriter Systems · Los datos incluidos son sintéticos y no representan a personas ni empresas reales.
