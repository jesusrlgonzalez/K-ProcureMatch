"""Generador determinista de cuentas por pagar SINTÉTICAS, no operaciones reales."""
from __future__ import annotations
import csv, random
from datetime import date,timedelta
from pathlib import Path

def store(path, fields, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='',encoding='utf-8') as f:
        wr=csv.DictWriter(f,fieldnames=fields);wr.writeheader();wr.writerows(rows)

def build(folder:Path, seed:int=1905, n:int=720)->dict:
    """Un PO/una recepción/una factura por línea, más defectos etiquetados.
    Todas las cantidades monetarias en centavos MXN: sin floats.
    """
    r=random.Random(seed); folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    vendors=[]; pos=[]; receipts=[]; invoices=[]; truth=[]
    for j in range(1,33):
        vendors.append({'supplier_id':f'S{j:03d}','legal_name':f'Proveedor de prueba {j:03d} SA', 'alias':f'Proveedor {j:03d}', 'currency':'MXN'})
    def invoice(po, idn, supplier=None, **kw):
        obj={'invoice_id':idn,'invoice_line':'1','supplier_ref':supplier or po['supplier_id'],
             'po_id':po['po_id'],'po_line':po['po_line'],'invoice_date':'2026-02-10',
             'qty_billed':po['qty_ordered'],'unit_price_cents':po['unit_price_cents'],
             'tax_cents':str(int(po['qty_ordered'])*int(po['unit_price_cents'])*16//100),
             'total_cents':str(int(po['qty_ordered'])*int(po['unit_price_cents'])*116//100),
             'currency':'MXN','document_type':'INVOICE','original_invoice':''}
        # amount: ensure exact integer arithmetic defined by floor(sales * 16 /100)
        sub=int(po['qty_ordered'])*int(po['unit_price_cents']);obj['total_cents']=str(sub+int(obj['tax_cents']))
        obj.update(kw);return obj
    for i in range(n):
        sup=f'S{(i%32)+1:03d}';qty=r.randint(2,50);price=r.randrange(2000,200000,100)
        po={'po_id':f'PO-{i+1:05d}', 'po_line':'1','supplier_id':sup,'qty_ordered':str(qty),
            'unit_price_cents':str(price),'currency':'MXN','po_date':'2026-01-05'}
        pos.append(po)
        receipts.append({'receipt_id':f'GR-{i+1:05d}','po_id':po['po_id'],'po_line':'1',
                         'qty_received':str(qty),'receipt_date':'2026-02-03'})
        invoices.append(invoice(po,f'INV-{i+1:05d}'))
        truth.append({'row_no':str(len(invoices)),'fault':'CLEAN','expected':'MATCH'})
    # Synthetic anomalies. Dirty clone references original PO but unique invoice IDs except for duplicates.
    faults=['DUPLICATE_INVOICE','UNKNOWN_SUPPLIER','SUPPLIER_MISMATCH','WRONG_CURRENCY',
            'PRICE_MISMATCH','QTY_EXCEEDS_RECEIPT','BAD_DATE','MISSING_INVOICE_NUMBER',
            'WRONG_TOTAL','NEGATIVE_QTY','INVOICE_BEFORE_ORDER','UNKNOWN_PO',
            'CREDIT_NOTE_WITHOUT_LINK','ALIAS_CANONICAL','UNRECEIVED_PO','INVALID_TAX']
    for k,fault in enumerate(faults):
        for rep in range(4):
            idx=100+(k*4+rep);po=pos[idx]; inv=invoice(po,f'ANOM-{k+1:02d}-{rep+1:02d}')
            if fault=='DUPLICATE_INVOICE': inv=dict(invoices[idx])
            elif fault=='UNKNOWN_SUPPLIER': inv['supplier_ref']='S-NOEXISTE'
            elif fault=='SUPPLIER_MISMATCH': inv['supplier_ref']='S032' if po['supplier_id']!='S032' else 'S031'
            elif fault=='WRONG_CURRENCY': inv['currency']='USD'
            elif fault=='PRICE_MISMATCH':inv['unit_price_cents']=str(int(inv['unit_price_cents'])+250)
            elif fault=='QTY_EXCEEDS_RECEIPT':inv['qty_billed']=str(int(inv['qty_billed'])+10)
            elif fault=='BAD_DATE':inv['invoice_date']='2026-99-99'
            elif fault=='MISSING_INVOICE_NUMBER':inv['invoice_id']=''
            elif fault=='WRONG_TOTAL':inv['total_cents']=str(int(inv['total_cents'])+320)
            elif fault=='NEGATIVE_QTY':inv['qty_billed']='-5'
            elif fault=='INVOICE_BEFORE_ORDER':inv['invoice_date']='2025-12-15'
            elif fault=='UNKNOWN_PO':inv['po_id']='PO-UNKNOWN'
            elif fault=='CREDIT_NOTE_WITHOUT_LINK':inv['document_type']='CREDIT_NOTE';inv['qty_billed']='-1'
            elif fault=='ALIAS_CANONICAL':inv['supplier_ref']=vendors[int(po['supplier_id'][1:])-1]['alias']
            elif fault=='UNRECEIVED_PO':inv['qty_billed']=str(int(inv['qty_billed'])+1)
            elif fault=='INVALID_TAX':inv['tax_cents']=str(int(inv['tax_cents'])+99)
            invoices.append(inv)
            truth.append({'row_no':str(len(invoices)),'fault':fault,
                          'expected':'REVIEW' if fault in ('ALIAS_CANONICAL','CREDIT_NOTE_WITHOUT_LINK') else 'BLOCK'})
    store(folder/'vendors.csv',['supplier_id','legal_name','alias','currency'],vendors)
    store(folder/'purchase_orders.csv',list(pos[0]),pos)
    store(folder/'goods_receipts.csv',list(receipts[0]),receipts)
    store(folder/'invoice_lines_dirty.csv',list(invoices[0]),invoices)
    store(folder/'INJECTION_TRUTH_NOT_FOR_SCORING.csv',list(truth[0]),truth)
    return {'suppliers':len(vendors),'po_lines':len(pos),'receipts':len(receipts),
            'invoice_lines':len(invoices),'synthetic_defect_rows':len(invoices)-n,'seed':seed}
