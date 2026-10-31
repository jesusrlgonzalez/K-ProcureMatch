"""Reglas conservadoras de revisión 3-way; nunca autoriza pagos automáticamente.
Ejecución determinista y sin alterar CSV originales. Solo MXN; montos en centavos.
"""
from __future__ import annotations
import csv, json, hashlib, argparse
from pathlib import Path
from datetime import date
from collections import Counter,defaultdict

def read_csv(f):
    with Path(f).open(newline='',encoding='utf-8-sig') as p:return list(csv.DictReader(p))
def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def integer(s):
    try:
        if not isinstance(s,str) or not s or not s.lstrip('-').isdigit():return None
        return int(s)
    except (ValueError,TypeError):return None
def day(s):
    try:return date.fromisoformat(s)
    except (ValueError,TypeError):return None

def reconcile(directory):
    directory=Path(directory)
    sources={name:directory/name for name in ('vendors.csv','purchase_orders.csv','goods_receipts.csv','invoice_lines_dirty.csv')}
    for n,path in sources.items():
        if not path.is_file():raise FileNotFoundError(f'FALTA_FUENTE:{n}')
    vendors=read_csv(sources['vendors.csv']);pos=read_csv(sources['purchase_orders.csv'])
    rec=read_csv(sources['goods_receipts.csv']);inv=read_csv(sources['invoice_lines_dirty.csv'])
    canonical={};alias_collision=set()
    for s in vendors:
        for label in (s['supplier_id'],s['alias']):
            key=label.casefold().strip()
            if key in canonical and canonical[key]!=s['supplier_id']:alias_collision.add(key)
            canonical[key]=s['supplier_id']
    poindex=defaultdict(list)
    for p in pos:poindex[(p['po_id'],p['po_line'])].append(p)
    recindex=defaultdict(list)
    for r in rec:recindex[(r['po_id'],r['po_line'])].append(r)
    seen=Counter((r['supplier_ref'].strip().casefold(),r['invoice_id'].strip().casefold()) for r in inv if r['invoice_id'].strip())
    logs=[];counters=Counter()
    for i,row in enumerate(inv,1):
        codes=[];key=(row['po_id'],row['po_line']); candidates=poindex[key]
        invoiceid=row['invoice_id'].strip()
        if not invoiceid:codes.append('MISSING_INVOICE_ID')
        elif seen[(row['supplier_ref'].strip().casefold(),invoiceid.casefold())]>1:codes.append('DUPLICATE_INVOICE_KEY')
        if not row['supplier_ref'].strip():codes.append('MISSING_SUPPLIER')
        supkey=row['supplier_ref'].casefold().strip()
        if supkey in alias_collision:codes.append('AMBIGUOUS_SUPPLIER_ALIAS')
        sup=canonical.get(supkey)
        if sup is None:codes.append('UNKNOWN_SUPPLIER')
        elif supkey!=sup.casefold():codes.append('SUPPLIER_ALIAS_REQUIRES_REVIEW')
        if len(candidates)!=1: codes.append('MISSING_OR_AMBIGUOUS_PO')
        po=candidates[0] if len(candidates)==1 else None
        if po and sup and po['supplier_id']!=sup:codes.append('SUPPLIER_PO_CONFLICT')
        if row['currency']!='MXN' or (po and po['currency']!='MXN'):codes.append('CURRENCY_NOT_SUPPORTED')
        invdate=day(row['invoice_date'])
        if invdate is None:codes.append('INVALID_INVOICE_DATE')
        if po and invdate and (pd:=day(po['po_date'])) and invdate<pd:codes.append('INVOICE_BEFORE_PO')
        qty=integer(row['qty_billed']);unit=integer(row['unit_price_cents']);tax=integer(row['tax_cents']);total=integer(row['total_cents'])
        if qty is None or (qty<=0 and row['document_type']!='CREDIT_NOTE'):codes.append('INVALID_QTY')
        if unit is None or unit<0:codes.append('INVALID_UNIT_PRICE')
        if tax is None or tax<0:codes.append('INVALID_TAX_AMOUNT')
        if total is None or total<0:codes.append('INVALID_TOTAL_AMOUNT')
        if po and unit is not None and unit!=integer(po['unit_price_cents']):codes.append('UNIT_PRICE_MISMATCH')
        if row['document_type']=='INVOICE' and None not in (qty,unit,tax,total) and qty>0 and unit>=0 and tax>=0 and total>=0:
            expected_tax=qty*unit*16//100
            if tax!=expected_tax:codes.append('TAX_MISMATCH_ILLUSTRATIVE_RATE')
            if total!=qty*unit+tax:codes.append('TOTAL_MISMATCH')
        recep=recindex[key]
        available=sum(integer(x['qty_received']) or 0 for x in recep)
        if po and (qty is not None and qty>available):codes.append('BILLED_QTY_EXCEEDS_RECEIVED')
        if po and (not recep or available<=0):codes.append('NO_RECEIPT')
        if row['document_type']!='INVOICE':
            if row['document_type']=='CREDIT_NOTE':codes.append('CREDIT_NOTE_MANUAL_REVIEW')
            else:codes.append('UNKNOWN_DOCUMENT_TYPE')
        if row['document_type']=='CREDIT_NOTE' and not row['original_invoice']:codes.append('CREDIT_NOTE_WITHOUT_REFERENCE')
        # Identificación de fecha de recepción, fiscalidad y factura cruzada pendiente para ERP real.
        blocking={'MISSING_INVOICE_ID','DUPLICATE_INVOICE_KEY','MISSING_SUPPLIER','AMBIGUOUS_SUPPLIER_ALIAS',
          'UNKNOWN_SUPPLIER','MISSING_OR_AMBIGUOUS_PO','SUPPLIER_PO_CONFLICT','CURRENCY_NOT_SUPPORTED',
          'INVALID_INVOICE_DATE','INVOICE_BEFORE_PO','INVALID_QTY','INVALID_UNIT_PRICE','INVALID_TAX_AMOUNT',
          'INVALID_TOTAL_AMOUNT','UNIT_PRICE_MISMATCH','TAX_MISMATCH_ILLUSTRATIVE_RATE','TOTAL_MISMATCH',
          'BILLED_QTY_EXCEEDS_RECEIVED','NO_RECEIPT','UNKNOWN_DOCUMENT_TYPE'}
        status='BLOCK' if any(c in blocking for c in codes) else ('REVIEW' if codes else 'MATCH_CANDIDATE')
        codes=sorted(set(codes));counters[status]+=1
        logs.append({'row_no':i,'invoice_id':row['invoice_id'],'supplier_ref':row['supplier_ref'],
          'po_id':row['po_id'],'po_line':row['po_line'],'status':status,'reason_codes':';'.join(codes)})
    assert sum(counters.values())==len(inv)
    return {'counts':dict(sorted(counters.items())),'total_rows':len(inv),'checks':{
      'row_reconciliation':sum(counters.values())==len(inv),
      'source_sha256':{name:digest(path) for name,path in sorted(sources.items())},
      'auto_payment_authorized':False,
      'tax_rule_is_illustrative_not_legal':True,
      'business_release_allowed':False}},logs

def save(directory,out):
    report,records=reconcile(directory);out=Path(out);out.mkdir(parents=True,exist_ok=True)
    (out/'quality_report.json').write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')
    for group,file in [('all','decision_ledger.csv'),('BLOCK','quarantine.csv'),('REVIEW','manual_review.csv'),('MATCH_CANDIDATE','match_candidates.csv')]:
        rows=records if group=='all' else [r for r in records if r['status']==group]
        with (out/file).open('w',encoding='utf-8',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(records[0]));w.writeheader();w.writerows(rows)
    return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    print(json.dumps(save(a.source,a.out),sort_keys=True,ensure_ascii=False))
if __name__=='__main__':main()
