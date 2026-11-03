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
        details.append(f"{progress['indexed_slots_per_second']:.2f} indexed slots/s (last step)")
    if 'lag_slots' in progress:
        label='lag' if progress.get('lag_is_current') is True else 'last sampled lag'
        details.append(f"{label} {progress['lag_slots']} slots")
    if isinstance(progress.get('head_sample_age_seconds'),(int,float)):
        details.append(f"head sample {progress['head_sample_age_seconds']:.0f}s old")
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
        text=f"{family.upper()}: {model.get('status','unknown')}"+(' ('+model['reason']+')' if model.get('reason') else '')
        if model.get('id'):text+=' candidate '+str(model['id'])[:10]
        if 'training_evidence' in model:text+=' | evidence '+str(model['training_evidence'])
        evaluation=model.get('historical_evaluation') or {}
        if isinstance(evaluation,dict):
            if isinstance(evaluation.get('brier'),(int,float)):text+=f" | historical Brier {evaluation['brier']:.3f}"
            net=evaluation.get('test_net') or {}
            if isinstance(net,dict) and isinstance(net.get('policy_expectancy'),(int,float)):
                text+=f" | held-out expectancy {net['policy_expectancy']:+.4f} ({net.get('status','collecting')})"
                if isinstance(net.get('policy_drawdown'),(int,float)):text+=f" | drawdown {net['policy_drawdown']:.4f}"
        details.append(text)
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
