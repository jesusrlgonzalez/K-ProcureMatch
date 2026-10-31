"""Evaluador fail-closed: jamás aprueba por presencia de archivos o autopresentación."""
from pathlib import Path
import json,argparse
GATES=('G0','G1','G2','G3','G4','G5_TECH','G5_BUSINESS')
def evaluate(root, gate):
    if gate not in GATES:raise ValueError('GATE_DESCONOCIDO')
    base=Path(root)/'EVIDENCIAS'/gate
    approval=base/'independent_approval.json'
    if not approval.is_file():return {'gate':gate,'status':'BLOCK','reason':'MISSING_INDEPENDENT_APPROVAL'}
    try: data=json.loads(approval.read_text(encoding='utf-8'))
    except (ValueError,OSError):return {'gate':gate,'status':'BLOCK','reason':'INVALID_APPROVAL_RECORD'}
    must=('implementer','reviewer','evidence_manifest_sha256','decision')
    if any(not data.get(k) for k in must):return {'gate':gate,'status':'BLOCK','reason':'INCOMPLETE_APPROVAL'}
    if data['implementer']==data['reviewer']:return {'gate':gate,'status':'BLOCK','reason':'SELF_APPROVAL_NOT_ALLOWED'}
    # v1 does NOT cryptographically attest approvals; they require manual review anyway.
    return {'gate':gate,'status':'REVIEW_REQUIRED','reason':'UNVERIFIED_HUMAN_APPROVAL'}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--project-root',default='.');p.add_argument('--gate',required=True,choices=GATES);a=p.parse_args()
 print(json.dumps(evaluate(a.project_root,a.gate),sort_keys=True))
