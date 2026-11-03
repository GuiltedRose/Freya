"""Opt-in selective acquisition bridge; hints never establish complete coverage."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import resource
import time

FORMAT='freya-selective-v1'


def owned_directory(path):
    path=Path(path)
    if path.is_symlink():raise ValueError('Symlink state directory refused')
    marker=path/'owner.json'
    if path.exists() and not marker.exists() and any(path.iterdir()):
        raise ValueError('Use an empty dedicated node directory; existing data preserved')
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    if marker.exists():
        if marker.is_symlink() or json.loads(marker.read_text())!={'format':FORMAT}:
            raise ValueError('Unrecognized state ownership')
    else:
        with os.fdopen(os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w') as stream:
            json.dump({'format':FORMAT},stream);stream.flush();os.fsync(stream.fileno())
    return path


def source_config(config,mode):
    # No source can qualify itself via a JSON boolean or a model-training job.
    if mode=='native':raise ValueError('Local producer qualification is not implemented; native-only blocked')
    from pipeline_runtime import read_json,resolve_indexer
    source=resolve_indexer(read_json(os.environ.get('FREYA_NODE_EXTERNAL_CONFIG') or config['external_config']),{'SOLANA_RPC_URL':os.environ.get('SOLANA_RPC_URL','')})
    endpoints=[e for e in source.get('endpoints',[]) if e.get('enabled') is True]
    if len(endpoints)!=1:raise ValueError('Select one reviewed source; no implicit endpoint rotation')
    source['endpoints']=endpoints
    endpoint=endpoints[0]
    if endpoint.get('network_mode')!=os.environ.get('FREYA_NODE_NETWORK_MODE',config.get('network_mode','remote_provider')):
        raise ValueError('Configured source and deployment network modes differ')
    if endpoint.get('backend')!='rust' or endpoint.get('ws_backend','rust')!='rust':
        raise ValueError('Explicit Rust HTTP and WebSocket backend required')
    endpoint.setdefault('pending_capacity',1024)
    endpoint.setdefault('observation_limit',100000)
    # Existing validation checks schemas, program pins, rights and stream budgets.
    from runtime_health import validate_config
    from onchain_stream import validate_ws_endpoint
    validate_config(source);validate_ws_endpoint(endpoint)
    if endpoint.get('policy',{}).get('stream_verified') is not True:
        raise ValueError('Reviewed streaming permission required')
    return source


def publish(path,record):
    temporary=path.with_suffix('.pending')
    with os.fdopen(os.open(temporary,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600),'w') as stream:
        json.dump(record,stream,allow_nan=False);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


async def run(config,directory,mode,max_cycles=None):
    from provider_limits import provider_limits_path,ProviderBudget
    import provider_limits
    configured=config.get('provider_ledger');shared=os.environ.get('CRYPTO_BOT_PROVIDER_LIMITS')
    if configured and shared and Path(configured).resolve()!=Path(shared).resolve():
        raise ValueError('Shared provider ledger mismatch')
    ledger=Path(shared or configured or provider_limits_path(Path('state/onchain/index.sqlite'))).resolve()
    os.environ['CRYPTO_BOT_PROVIDER_LIMITS']=str(ledger)
    os.environ['CRYPTO_BOT_RPC_CONSUMER']='indexer'
    provider_limits.BUDGET=ProviderBudget(ledger)
    source=source_config(config,mode)
    from solana_observer import observe
    from pipeline_runtime import writer_lock
    from runtime_health import health
    database=directory/'index.sqlite'
    if database.is_symlink():raise ValueError('Symlink database refused')
    identity=provider_limits.BUDGET.identity(source['endpoints'][0]['url'])
    status_path=directory/'status.json'
    disk_limit=config.get('disk_bytes',64*1024**3)
    if type(disk_limit) is not int or not 1024**3<=disk_limit<=1024**4:raise ValueError('Invalid disk budget')
    def snapshot():
        import sqlite3
        try:state=health(database) if database.exists() else {}
        except (ValueError,OSError,sqlite3.Error):state={}
        disk=sum(p.stat().st_size for p in directory.iterdir() if p.is_file() and not p.is_symlink())
        usage=resource.getrusage(resource.RUSAGE_SELF)
        record={'format':FORMAT,'at':time.time(),'active_source':'external',
                'mode':mode,'source_identity':identity,'native_qualified':False,'automatic_fallback':mode=='automatic',
                'fallback_reason':'native producer not qualified' if mode=='automatic' else None,
                'verification_level':'upstream_claimed_execution_and_finality',
                'independent_consensus_verified':False,'data_ready':state.get('ready',False),
                'progress':state.get('progress'),'coverage':state.get('coverage'),
                'memory_peak_bytes':usage.ru_maxrss*(1 if __import__('sys').platform=='darwin' else 1024),
                'cpu_seconds':usage.ru_utime+usage.ru_stime,'disk_bytes':disk,
                'traffic_accounting_scope':'existing provider ledger and namespace counters; no invented wire estimates',
                'protocol_bytes':None,'retransmission_bytes':None,'relevant_bytes':None,
                'irrelevant_bytes':None,'events_per_mb':None,'decoder_errors':None,
                'relevant_transactions_per_second':None,'verified_events_per_second':None,
                'sync_bytes_per_second':None,'total_bandwidth_bytes':None,
                'http_429_count':None,'cooldown_seconds':provider_limits.BUDGET.cooldown_remaining(source['endpoints'][0]['url']),
                'queue_pressure':None,'cache_pressure':None,'missing_slot_recovery':'existing finalized manifest/checkpoint recovery',
                'provider_ledger':str(ledger),'whole_system_memory_verified':False}
        publish(status_path,record)
        if disk>=disk_limit:raise RuntimeError('Disk budget exhausted; preserve data and recover explicitly')
    with writer_lock(database,'indexer',source):
        task=asyncio.create_task(observe(source,database,batch=8,interval=5,resume=True,max_cycles=max_cycles))
        try:
            while not task.done():
                await asyncio.sleep(1)
                snapshot()
            await task
            snapshot()
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--state-dir',type=Path,default=Path('state/selective_node'))
    parser.add_argument('--mode',choices=('native','external','automatic'))
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--max-cycles',type=int)
    args=parser.parse_args()
    if args.status:
        path=args.state_dir/'status.json'
        if path.is_symlink():parser.error('Symlink status refused')
        record=json.loads(path.read_text()) if path.is_file() else {'format':FORMAT,'status':'not_started','native_qualified':False}
        if record.get('format')!=FORMAT:parser.error('Unrecognized status format')
        print(json.dumps(record));return
    if not args.config:parser.error('--config required; no default connections')
    config=json.loads(args.config.read_text());mode=args.mode or config.get('mode','external')
    if mode not in ('native','external','automatic'):parser.error('Invalid source mode')
    if mode=='native':parser.error('Native producer is unqualified; no external fallback in native-only mode')
    memory=config.get('memory_bytes',4*1024**3)
    if type(memory) is not int or not 512*1024**2<=memory<=32*1024**3:parser.error('Invalid memory budget')
    if args.max_cycles is not None and not 1<=args.max_cycles<=10000:parser.error('Invalid cycle bound')
    resource.setrlimit(resource.RLIMIT_AS,(memory,memory))
    directory=owned_directory(args.state_dir)
    try:asyncio.run(run(config,directory,mode,args.max_cycles))
    except KeyboardInterrupt:pass
    except Exception as error:
        publish(directory/'status.json',{'format':FORMAT,'at':time.time(),'status':'blocked',
                'error_type':type(error).__name__,'native_qualified':False,'data_ready':False})
        raise SystemExit('Selective node blocked; configuration/evidence review required (endpoint omitted)') from None


if __name__=='__main__':main()
