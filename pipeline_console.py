"""Follow owned worker logs without suppressing the existing trade/ML consoles."""
import json
from pathlib import Path
import re


def safe_line(value):
    value=re.sub(r'https?://\S+|wss?://\S+','[endpoint]',value)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]','',value)[:16384]


def format_pipeline(record):
    status=record.get('status','unknown').replace('_',' ')
    if record.get('role'):return f"PIPELINE | {record['role']} started | log {record.get('log','')}"
    progress=record.get('progress') or {}
    details=[]
    if isinstance(progress.get('indexed_slots_per_second'),(int,float)):
        details.append(f"{progress['indexed_slots_per_second']:.2f} indexed slots/s")
    if 'lag_slots' in progress:details.append(f"lag {progress['lag_slots']} slots")
    if record.get('failed_role'):details.append(record['failed_role']+' exited '+str(record.get('exit_code')))
    details.extend(record.get('blockers',[]))
    if record.get('reason'):details.append(record['reason'])
    if record.get('startup_error'):details.append(record['startup_error'])
    return safe_line('PIPELINE | '+status+(' | '+'; '.join(details) if details else ''))


def format_learning(record):
    pipeline=record.get('pipeline') or {}
    if not isinstance(pipeline,dict):return 'ML | unexpected report shape; inspect learning.log'
    details=[]
    for ledger in pipeline.get('ledgers',[]):
        details.append(str(ledger.get('source','ledger'))+': '+str(ledger.get('status',str(ledger.get('imported',0))+' imported')))
    models=pipeline.get('models') or {}
    if not isinstance(models,dict):return 'ML | unexpected model report; inspect learning.log'
    for family,model in models.items():
        if not isinstance(model,dict):continue
        details.append(f"{family.upper()}: {model.get('status','unknown')}"+(' ('+model['reason']+')' if model.get('reason') else ''))
    return safe_line('ML | '+' | '.join(details or ['batch completed']))


class WorkerOutput:
    """Bounded incremental reads; append-only logs retain the complete evidence."""
    def __init__(self,json_output=False):
        self.json_output=json_output;self.logs={}

    def follow(self,role,path):
        path=Path(path)
        self.logs[role]={'path':path,'offset':path.stat().st_size if path.exists() else 0,'pending':b''}

    def poll(self):
        for role,row in self.logs.items():
            try:
                with row['path'].open('rb') as stream:
                    if stream.seek(0,2)<row['offset']:row.update(offset=0,pending=b'')
                    stream.seek(row['offset']);data=stream.read(65536);row['offset']=stream.tell()
            except OSError:continue
            parts=(row['pending']+data).split(b'\n')
            row['pending']=parts.pop()[-16384:]
            for part in parts:
                line=part.decode('utf-8',errors='replace')[:16384]
                if not line.strip():continue
                if self.json_output:
                    print(json.dumps({'worker':role,'output':safe_line(line)}),flush=True)
                elif role=='consumer':print(safe_line(line),flush=True)
                elif role=='learning':
                    try:record=json.loads(line)
                    except ValueError:print('ML | '+safe_line(line),flush=True);continue
                    print(format_learning(record) if isinstance(record,dict) else 'ML | '+safe_line(line),flush=True)
                elif any(word in line for word in ('Error','Exception','Traceback')):
                    print('INDEXER | '+safe_line(line),flush=True)
