"""Private, bounded read-only RPC/paper evidence report; never makes network calls."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import time
from contextlib import closing


def namespace_settings():
    try:current=os.stat('/proc/self/ns/net').st_ino
    except OSError:current=None
    try:managed=os.stat('/run/netns/solana-budget').st_ino
    except OSError:managed=None
    return {'namespace_inode':current,'managed_namespace_match':current==managed if current is not None and managed is not None else None}


def effective_settings(rpc,identity=None):
    from provider_limits import THROTTLE_MARGIN_SECONDS
    return dict(identity or {},provider_ledger=str(rpc.budget.path),**namespace_settings(),
                network_mode=getattr(rpc,'endpoint',{}).get('network_mode','unknown'),
                backend='rust' if type(rpc).__name__=='RustIndexRpc' else 'python',
                request_interval_seconds=rpc.interval,block_batch_size=rpc.block_batch_size,
                burst_requests=rpc.burst_requests,burst_cooldown_seconds=rpc.burst_cooldown_seconds,
                throttle_margin_seconds=THROTTLE_MARGIN_SECONDS,
                request_size_policy='getBlock: full JSON transactions; batches at most two logical calls')


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)


def ledger_report(path,since):
    if not Path(path).is_file():return {'path':str(path),'available':False}
    with closing(readonly(path)) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        report={'path':str(path),'available':True,'historical_request_trace_available':'provider_events' in tables}
        if 'providers' in tables:
            # Legacy attempts is a failure streak, not a request count or 429 count.
            report['provider_state']=[{'blocked_until':r[0],'failure_streak':r[1],'denied':bool(r[2])}
                                      for r in db.execute('SELECT blocked,attempts,denied FROM providers')]
        if 'provider_events' in tables:
            db.row_factory=sqlite3.Row
            report['activity']=[dict(r) for r in db.execute('SELECT provider_id,endpoint_id,consumer,method,event,http_status,origin,count(*) AS records,sum(logical_calls) AS logical_units,min(at) AS first_at,max(at) AS last_at FROM provider_events WHERE at>=? GROUP BY provider_id,endpoint_id,consumer,method,event,http_status,origin',(since,))]
            report['recent_backoff']=[dict(r) for r in db.execute("SELECT at,pid,consumer,provider_id,endpoint_id,http_status,retry_after_seconds,blocked_until,origin FROM provider_events WHERE at>=? AND event='backoff' ORDER BY id DESC LIMIT 50",(since,))]
    return report


def observation(index_db):
    from runtime_health import health
    state=health(index_db)
    progress=state.get('progress') or {}
    markets=state.get('markets') or {}
    return {'at':time.time(),'ready':state['ready'],'blockers':state['blockers'],
            'index_watermark':state.get('index_watermark'),'index_age_seconds':state.get('index_age_seconds'),
            'progress':progress,'coverage':state.get('coverage'),
            'conversions':state.get('conversions'),
            'oracle':{'status':(state.get('oracle') or {}).get('status'),
                      'at':(state.get('oracle') or {}).get('at'),
                      'feeds':[{'publish_time':row.get('publish_time'),'failed':bool(row.get('error'))}
                               for row in (state.get('oracle') or {}).get('feeds',[])]},
            'market_count':len(markets),'usable_market_count':sum(not row.get('errors') and not row.get('error') for row in markets.values())}


def safe_settings(value):
    """Export only the public scalar settings contract from external log/report files."""
    if not isinstance(value,dict):return {}
    fields=('provider_id','endpoint_id','provider_ledger','namespace_inode','managed_namespace_match',
            'network_mode','backend','request_interval_seconds','block_batch_size','burst_requests',
            'burst_cooldown_seconds','throttle_margin_seconds')
    result={}
    for key in fields:
        item=value.get(key)
        if key in ('provider_id','endpoint_id'):
            if not isinstance(item,str) or len(item)!=64 or any(c not in '0123456789abcdef' for c in item):item=None
        elif isinstance(item,str):
            if '://' in item or len(item)>1024:item=None
        elif item is not None and type(item) not in (bool,int,float):item=None
        result[key]=item
    return result


def legacy_logs(directory):
    result={}
    for name in ('indexer','consumer','learning'):
        path=Path(directory)/(name+'.log')
        if not path.is_file():continue
        # Bounded tail only; old files never rewritten and raw lines never exported.
        with path.open('rb') as stream:
            size=path.stat().st_size;stream.seek(max(0,size-1_000_000));lines=stream.read(1_000_000).splitlines()
        counts={};records=[]
        for line in lines[-2000:]:
            try:row=json.loads(line)
            except (ValueError,UnicodeError):continue
            if not isinstance(row,dict):continue
            if row.get('http_status')==429:counts['http_429_records']=counts.get('http_429_records',0)+1
            if row.get('status')=='rpc_effective_settings':
                records.append(safe_settings(row.get('settings')))
        result[name]={'bounded_tail':True,'counts':counts,'effective_settings':records[-3:],
                      'timestamps_note':'Legacy records without at cannot be retrospectively timestamp-correlated'}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index-db',type=Path,required=True)
    parser.add_argument('--provider-ledger',type=Path)
    parser.add_argument('--log-dir',type=Path,default=Path('state/paper_volume/pipeline/paper'))
    parser.add_argument('--probe-report',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--duration-seconds',type=int,default=0)
    parser.add_argument('--interval-seconds',type=int,default=30)
    args=parser.parse_args()
    if not 0<=args.duration_seconds<=7200 or not 10<=args.interval_seconds<=60:parser.error('Duration 0..7200 seconds; interval 10..60 seconds')
    from provider_limits import provider_limits_path
    ledger=args.provider_ledger or provider_limits_path(args.index_db)
    start=time.time();deadline=time.monotonic()+args.duration_seconds
    fd=os.open(args.output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as out:
        def emit(row):out.write(json.dumps(row,allow_nan=False)+'\n');out.flush();os.fsync(out.fileno())
        emit({'kind':'baseline','at':start,'network_requests':0,'ledger':ledger_report(ledger,start-3600),
              'legacy_ledger':ledger_report(Path('state/provider_limits.sqlite'),start-3600) if Path(ledger).resolve()!=Path('state/provider_limits.sqlite').resolve() else None,
              'logs':legacy_logs(args.log_dir),**namespace_settings()})
        if args.probe_report:
            probe=json.loads(args.probe_report.read_text())
            emit({'kind':'probe_comparison','settings':[safe_settings(r.get('effective_settings')) for r in probe.get('results',[]) if r.get('effective_settings')],
                  'note':'Older probe reports without effective settings cannot establish credential/ledger equivalence'})
        first=None;last=None;ready_streak=0;accepted=False
        while True:
            sample=observation(args.index_db)
            cursor=(sample['progress'] or {}).get('next_slot')
            if first is None:first=cursor
            regressed=last is not None and cursor is not None and cursor<last
            last=cursor if cursor is not None else last
            ready=sample['ready'] and sample['usable_market_count']>0 and (sample['progress'] or {}).get('lag_is_current') is True and not regressed
            ready_streak=ready_streak+1 if ready else 0
            # Three consecutive complete/fresh snapshots plus real checkpoint advance.
            accepted=ready_streak>=3 and first is not None and last is not None and last>first
            elapsed=max(.001,time.time()-start)
            wall_rate=(cursor-first)/elapsed if cursor is not None and first is not None else None
            emit(dict(sample,kind='sample',checkpoint_regressed=regressed,readiness_streak=ready_streak,
                      wall_checkpoint_slots_per_second=wall_rate,
                      ledger=ledger_report(ledger,max(start,time.time()-args.interval_seconds))))
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            time.sleep(min(args.interval_seconds,remaining))
        emit({'kind':'summary','sustained_data_milestone_observed':accepted,'paper_ml_accepted':False,
              'live_ready':False,'network_requests':0,'duration_seconds':time.time()-start,
              'note':'Polling cannot prove intermediate states; inspect contiguous coverage, block issues and causal conversions. No trades or labels manufactured.'})
    print(json.dumps({'report':str(args.output),'sustained_data_milestone_observed':accepted}))
    if args.duration_seconds and not accepted:raise SystemExit(1)


if __name__=='__main__':main()
