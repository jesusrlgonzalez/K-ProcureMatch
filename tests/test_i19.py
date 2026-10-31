import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from i19.generator import build
from i19.reconcile import reconcile, save, read_csv
from i19.gates import evaluate

class I19Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.source=Path(cls.tmp.name)/'input'
        build(cls.source,seed=1905,n=240)
        cls.report,cls.log=reconcile(cls.source)
        cls.truth=read_csv(cls.source/'INJECTION_TRUTH_NOT_FOR_SCORING.csv')
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def test_01_raw_rows_preserved(self):self.assertEqual(sum(self.report['counts'].values()),304)
    def test_02_no_payment_authority(self):self.assertFalse(self.report['checks']['auto_payment_authorized'])
    def test_03_reports_have_hashes(self):self.assertEqual(len(self.report['checks']['source_sha256']),4)
    def test_04_injected_duplicates_block(self):
        rows=[self.log[int(t['row_no'])-1] for t in self.truth if t['fault']=='DUPLICATE_INVOICE']
        self.assertEqual(len(rows),4)
        self.assertTrue(all(x['status']=='BLOCK' and 'DUPLICATE_INVOICE_KEY' in x['reason_codes'] for x in rows))
    def test_05_unknown_suppliers_block(self):
        self.assertTrue(all(self.log[int(t['row_no'])-1]['status']=='BLOCK' for t in self.truth if t['fault']=='UNKNOWN_SUPPLIER'))
    def test_06_aliases_manual_review(self):
        self.assertTrue(all(self.log[int(t['row_no'])-1]['status']=='REVIEW' for t in self.truth if t['fault']=='ALIAS_CANONICAL'))
    def test_07_credit_notes_review(self):
        self.assertTrue(all(self.log[int(t['row_no'])-1]['status']=='REVIEW' for t in self.truth if t['fault']=='CREDIT_NOTE_WITHOUT_LINK'))
    def test_08_wrong_currency_blocks(self):
        self.assertTrue(all(self.log[int(t['row_no'])-1]['status']=='BLOCK' for t in self.truth if t['fault']=='WRONG_CURRENCY'))
    def test_09_wrong_totals_block(self):
        self.assertTrue(all('TOTAL_MISMATCH' in self.log[int(t['row_no'])-1]['reason_codes'] for t in self.truth if t['fault']=='WRONG_TOTAL'))
    def test_10_date_errors_block(self):
        self.assertTrue(all(self.log[int(t['row_no'])-1]['status']=='BLOCK' for t in self.truth if t['fault'] in ('BAD_DATE','INVOICE_BEFORE_ORDER')))
    def test_11_quantity_received_block(self):
        self.assertTrue(all(self.log[int(t['row_no'])-1]['status']=='BLOCK' for t in self.truth if t['fault']=='QTY_EXCEEDS_RECEIPT'))
    def test_12_price_mismatch_block(self):
        self.assertTrue(all('UNIT_PRICE_MISMATCH' in self.log[int(t['row_no'])-1]['reason_codes'] for t in self.truth if t['fault']=='PRICE_MISMATCH'))
    def test_13_invalid_tax_block(self):
        self.assertTrue(all('TAX_MISMATCH_ILLUSTRATIVE_RATE' in self.log[int(t['row_no'])-1]['reason_codes'] for t in self.truth if t['fault']=='INVALID_TAX'))
    def test_14_missing_po_block(self):
        self.assertTrue(all('MISSING_OR_AMBIGUOUS_PO' in self.log[int(t['row_no'])-1]['reason_codes'] for t in self.truth if t['fault']=='UNKNOWN_PO'))
    def test_15_negative_qty_block(self):
        self.assertTrue(all(self.log[int(t['row_no'])-1]['status']=='BLOCK' for t in self.truth if t['fault']=='NEGATIVE_QTY'))
    def test_16_missing_invoice_id_block(self):
        self.assertTrue(all(self.log[int(t['row_no'])-1]['status']=='BLOCK' for t in self.truth if t['fault']=='MISSING_INVOICE_NUMBER'))
    def test_17_rebuild_is_identical(self):
        with tempfile.TemporaryDirectory() as t:
            a=Path(t)/'a';b=Path(t)/'b';save(self.source,a);save(self.source,b)
            for name in ('quality_report.json','decision_ledger.csv','quarantine.csv','manual_review.csv','match_candidates.csv'):
                self.assertEqual(hashlib.sha256((a/name).read_bytes()).digest(),hashlib.sha256((b/name).read_bytes()).digest())
    def test_18_source_unchanged(self):
        prior={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.glob('*.csv')}
        with tempfile.TemporaryDirectory() as t:save(self.source,Path(t))
        after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.glob('*.csv')}
        self.assertEqual(prior,after)
    def test_19_generate_deterministic(self):
        with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b:
            build(Path(a),1905,200);build(Path(b),1905,200)
            self.assertEqual((Path(a)/'invoice_lines_dirty.csv').read_bytes(),(Path(b)/'invoice_lines_dirty.csv').read_bytes())
    def test_20_gate_missing_evidence_block(self):
        with tempfile.TemporaryDirectory() as d:self.assertEqual(evaluate(d,'G5_TECH')['status'],'BLOCK')
    def test_21_gate_self_sign_block(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'EVIDENCIAS/G1';p.mkdir(parents=True)
            (p/'independent_approval.json').write_text(json.dumps({'implementer':'a','reviewer':'a','decision':'PASS','evidence_manifest_sha256':'test'}))
            self.assertEqual(evaluate(d,'G1')['status'],'BLOCK')
    def test_22_no_automatic_gate_pass_even_fake_approval(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'EVIDENCIAS/G1';p.mkdir(parents=True)
            (p/'independent_approval.json').write_text(json.dumps({'implementer':'a','reviewer':'b','decision':'PASS','evidence_manifest_sha256':'test'}))
            self.assertEqual(evaluate(d,'G1')['status'],'REVIEW_REQUIRED')
    def test_23_missing_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):reconcile(d)
    def test_24_truth_not_in_scoring(self):
        with tempfile.TemporaryDirectory() as d:
            copied=Path(d)/'src';copied.mkdir()
            for name in ('vendors.csv','purchase_orders.csv','goods_receipts.csv','invoice_lines_dirty.csv'):
                (copied/name).write_bytes((self.source/name).read_bytes())
            report,_=reconcile(copied);self.assertEqual(report['counts'],self.report['counts'])

if __name__=='__main__':unittest.main()
