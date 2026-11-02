"""Coordinated, explicit paper/live startup. Never promotes models or clears state."""
import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from config import public_env
from pipeline_runtime import attached,check_index_target,paper_state_directory,preflight,read_json,resolve_indexer
from runtime_health import safe_reason

ROOT=Path(__file__).resolve().parent


def clean_environment():
    environment=dict(os.environ)
    for key in ('SOLANA_PRIVATE_KEY','SOLANA_KEYPAIR_PATH','SOLANA_ENV_FILE'):
        environment.pop(key,None)
    return environment


def stop(process):
    if process.poll() is not None:return
    process.send_signal(signal.SIGINT)
    try:process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


def template_documents(args):
    """Public defaults only; no inferred approvals or deployment facts."""
    indexer=read_json(ROOT/'onchain_pipeline.example.json')
    indexer.pop('schema_directory',None)
    source=indexer['endpoints'][0]
    source.update(source_id='reviewed-mainnet-source-v1',url='${SOLANA_RPC_URL}',
                  source_kind='solana_rpc',network_mode='remote_provider')
    source.pop('network_budget',None)
    source['policy'].update(remote_provider_verified=False,
        allowed_rpc_hosts=['api.mainnet.solana.com','api.mainnet-beta.solana.com'])
    indexer['oracle']['source_id']=source['source_id']
    indexer['reviewed_program_versions']={}
    for name in ('pump','pump_amm'):
        manifest=read_json(ROOT/'vendor'/(name+'.manifest.json'))
        indexer['reviewed_program_versions'][manifest['program']]=[{
            'from_slot':None,'through_slot':None,'deployment_slot':None,'idl_hash':manifest['sha256']}]
    learning=read_json(ROOT/'learning_pipeline.example.json')
    learning['ledgers']=[{'source':'bot_paper','path':str(args.paper_state/'paper.sqlite')},
                         {'source':'bot_live','path':str(args.live_state/'meme_live.sqlite')}]
    return indexer,learning


def prepare_files(args,*,emit=True):
    """Create review templates exclusively; never assert rights or deployment facts."""
    indexer,learning=template_documents(args)
    results=[]
    for path,document in ((args.indexer_config,indexer),(args.learning_config,learning)):
        if path.exists():results.append({'file':str(path),'status':'existing_preserved'});continue
        path.parent.mkdir(parents=True,exist_ok=True)
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w') as stream:json.dump(document,stream,indent=2);stream.write('\n')
        results.append({'file':str(path),'status':'created_unapproved_review_template'})
    result={'configuration':results,'activated':False,'review_required':True}
    if emit:print(json.dumps(result))
    return result


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('--mode',choices=('paper','live','indexer','data'),default='paper')
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    parser.add_argument('--indexer-config','--config',type=Path)
    parser.add_argument('--learning-config',type=Path)
    parser.add_argument('--onchain-db',type=Path)
    parser.add_argument('--unified-db',type=Path)
    parser.add_argument('--state-dir',type=Path)
    parser.add_argument('--paper-state-dir',type=Path)
    parser.add_argument('--live-state-dir',type=Path)
    parser.add_argument('--capital-config',type=Path,default=ROOT/'capital_policy.example.json')
    parser.add_argument('--preflight',action='store_true')
    parser.add_argument('--status',action='store_true',help='Read the saved pipeline status without starting workers')
    parser.add_argument('--prepare-config',action='store_true')
    parser.add_argument('--configure',action='store_true',help='Guided first-run setup: explicit source review, read-only RPC discovery and saved configuration; starts no services')
    parser.add_argument('--initialize',action='store_true')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--manage-only',action='store_true',help='Live exits/reconciliation only; unavailable ML cannot prevent this recovery path')
    parser.add_argument('--external-indexer',action='store_true')
    parser.add_argument('--external-workers',action='store_true',help='Use independently supervised data workers; do not own or stop them')
    parser.add_argument('--learning-interval',type=int,default=900)
    args,extra=parser.parse_known_args()
    args.json_console='--json' in extra
    public=public_env(args.env_file)
    def path(value,key,default):return Path(value or public.get(key) or default).resolve()
    args.indexer_config=path(args.indexer_config,'CRYPTO_BOT_INDEXER_CONFIG','onchain_pipeline.json')
    args.learning_config=path(args.learning_config,'CRYPTO_BOT_LEARNING_CONFIG','learning_pipeline.json')
    args.onchain_db=path(args.onchain_db,'CRYPTO_BOT_INDEX_DB','state/onchain/index.sqlite')
    args.unified_db=path(args.unified_db,'CRYPTO_BOT_UNIFIED_DB','state/ml/unified.sqlite')
    paper_explicit=args.state_dir if args.mode=='paper' and args.state_dir else args.paper_state_dir
    args.paper_state=paper_state_directory(paper_explicit,public.get('CRYPTO_BOT_PAPER_STATE_DIR'))
    args.live_state=path(args.live_state_dir,'CRYPTO_BOT_LIVE_STATE_DIR','state/meme_live')
    if args.state_dir:
        if args.mode=='paper':args.paper_state=args.state_dir.resolve()
        elif args.mode=='live':args.live_state=args.state_dir.resolve()
        else:parser.error('--state-dir belongs to a paper/live consumer')
    if sum((args.prepare_config,args.configure,args.preflight,args.status))>1:parser.error('Choose configure, prepare-config, preflight or status separately')
    if args.status:
        directory=args.live_state if args.mode=='live' else args.paper_state
        status_file=directory/'pipeline'/args.mode/'status.json'
        print(json.dumps(read_json(status_file) if status_file.is_file() else
              {'status':'not_started','mode':args.mode,'services_started':False}))
        return
    if args.external_workers:args.external_indexer=True
    if args.configure:
        if args.initialize or args.execute:parser.error('--configure is setup only; do not combine initialization or execution')
        from pipeline_setup import configure
        configure(args,public,*template_documents(args))
        return
    if args.prepare_config:prepare_files(args);return
    if not 30<=args.learning_interval<=86400:parser.error('Learning interval must be 30..86400 seconds')
    if args.mode=='live' and not (args.execute or args.preflight):parser.error('Live startup requires explicit --execute; credentials alone do not authorize trading')
    if args.execute and args.mode!='live':parser.error('--execute is only valid with --mode live')
    if args.manage_only and args.mode!='live':parser.error('--manage-only belongs to the live recovery path')
    if args.initialize and args.mode!='paper':parser.error('--initialize creates a paper session only')
    learning_required=args.mode!='indexer' and not args.manage_only
    configuration_paths=[args.indexer_config]+([args.learning_config] if learning_required else [])
    missing=[str(candidate) for candidate in configuration_paths if not candidate.is_file()]
    from pipeline_setup import automatic_profile,template_needs_setup
    candidate=read_json(args.indexer_config) if args.indexer_config.is_file() else None
    automatic=automatic_profile(public,candidate)
    setup_needed=bool(missing) or (automatic and template_needs_setup(args.indexer_config,args.learning_config))
    if setup_needed and (automatic or sys.stdin.isatty()) and args.mode in ('paper','live') and not args.preflight:
        if args.initialize and (args.paper_state/'paper.sqlite').exists():parser.error('Paper state exists; omit --initialize to resume; nothing was reset')
        from pipeline_setup import configure
        configure(args,public,*template_documents(args))
        missing=[str(candidate) for candidate in configuration_paths if not candidate.is_file()]
    if missing:
        prepared=None
        if args.initialize:
            if (args.paper_state/'paper.sqlite').exists():parser.error('Paper state exists; omit --initialize to resume; nothing was reset')
            prepared=prepare_files(args,emit=False)
        print(json.dumps({'status':'configuration_required','mode':args.mode,
            'missing_before_setup':missing,'configuration':prepared,
            'services_started':False,'runtime_ready':False,'state_preserved':True,
            'next_steps':[
                'Run this runner with --configure for guided setup without editing JSON',
                'Run the same runner with --preflight to see remaining configuration blockers'],
            'documentation':'docs/coordinated_pipeline.md#first-configuration'}),flush=True)
        raise SystemExit(2)
    # Raw commands remain available for deliberate replay/repair; the coordinated
    # path never resets state or decouples a consumer from its configured index.
    if any(value.split('=',1)[0] in ('--reset','--feed','--unified-config','--start-slot','--resume') for value in extra):
        parser.error('Replay, resets and checkpoint repair use the explicit raw commands; coordinated startup preserves state')
    issues=[];indexer={};learning=None
    try:
        indexer=resolve_indexer(read_json(args.indexer_config),public)
        if indexer.get('schema_directory'):
            schema=Path(indexer['schema_directory'])
            indexer['schema_directory']=str(schema if schema.is_absolute() else (args.indexer_config.parent/schema).resolve())
    except (OSError,ValueError,KeyError,TypeError) as error:
        issues.append('indexer config: '+safe_reason(error)+'; use --prepare-config to create non-active review templates')
    if learning_required:
        try:
            learning=read_json(args.learning_config)
            for row in learning.get('ledgers',[]):
                candidate=Path(row['path'])
                row['path']=str(candidate if candidate.is_absolute() else (args.learning_config.parent/candidate).resolve())
        except (OSError,ValueError,KeyError,TypeError) as error:issues.append('learning config: '+safe_reason(error))
    # Invalid/unreadable input is not an empty valid configuration. Report its
    # actual parse error without manufacturing downstream missing-field errors.
    if issues:
        print(json.dumps({'status':'configuration_invalid','mode':args.mode,'blockers':issues,
            'services_started':False,'runtime_ready':False,'state_preserved':True}),flush=True)
        raise SystemExit(2)
    issues+=preflight(indexer,learning,training=learning_required,
        ledger_paths=([args.paper_state/'paper.sqlite',args.live_state/'meme_live.sqlite'] if learning_required else []))
    if args.mode!='indexer' and not indexer.get('oracle',{}).get('feeds'):
        issues.append('Reviewed USD oracle feeds are required for market/position valuation')
    if args.initialize and (args.paper_state/'paper.sqlite').exists():issues.append('Paper state exists; omit --initialize to resume; nothing was reset')
    if args.mode=='paper' and not args.preflight and not (args.paper_state/'paper.sqlite').exists() and not args.initialize:
        explicit_inventory=all(any(value==flag or value.startswith(flag+'=') for value in extra)
                               for flag in ('--balance','--native-sol','--sol-price'))
        if not explicit_inventory:issues.append('Fresh paper session requires --initialize or explicit --balance, --native-sol and --sol-price')
    if args.external_indexer and not args.onchain_db.is_file():issues.append('External data mode requires an existing index database')
    if args.external_workers and learning_required and not args.unified_db.is_file():issues.append('External data mode requires the model database schema to exist')
    try:
        from capital_policy import load_config
        load_config(args.capital_config)
    except (OSError,ValueError,TypeError) as error:issues.append('capital policy: '+safe_reason(error))
    try:
        check_index_target(args.onchain_db)
        if learning_required and args.unified_db.exists():
            from unified_observations import ObservationStore
            ObservationStore(args.unified_db,create=False)
    except (OSError,ValueError,sqlite3.Error) as error:issues.append('database target: '+safe_reason(error))
    result={'status':'blocked' if issues else 'preflight_passed','blockers':list(dict.fromkeys(issues)),
            'mode':args.mode,'services_started':False,'runtime_ready':False}
    if args.preflight or args.json_console or issues:print(json.dumps(result),flush=True)
    else:print('PIPELINE | configuration passed | starting '+args.mode+' services',flush=True)
    if issues:raise SystemExit(2)
    if args.preflight:return
    args.model_selection_configured=bool(learning and set(learning.get('candidates',{}))=={'svm','lstm','rag'})
    supervise(args,extra,indexer,learning)


def supervise(args,extra,indexer,learning):
    state=args.live_state if args.mode=='live' else args.paper_state
    directory=state/'pipeline'/args.mode;directory.mkdir(parents=True,exist_ok=True)
    children={};streams=[];stopping=False;startup_error=None
    from pipeline_console import WorkerOutput,format_pipeline
    output=WorkerOutput(getattr(args,'json_console',False));args.worker_output=output
    environment=clean_environment()
    from provider_limits import provider_limits_path
    environment['CRYPTO_BOT_PROVIDER_LIMITS']=str(provider_limits_path(args.onchain_db))
    def interrupt(*_):
        nonlocal stopping
        stopping=True
    old={sig:signal.signal(sig,interrupt) for sig in (signal.SIGINT,signal.SIGTERM)}
    def status(value):
        value=dict(value,at=time.time(),mode=args.mode,execution_requested=args.mode=='live' and args.execute)
        temporary=directory/'status.tmp'
        temporary.write_text(json.dumps(value,allow_nan=False))
        temporary.replace(directory/'status.json')
        print(json.dumps(value) if getattr(args,'json_console',False) else format_pipeline(value),flush=True)
    def spawn(role,script,arguments,env=environment):
        stream=(directory/(role+'.log')).open('ab');streams.append(stream)
        output.follow(role,directory/(role+'.log'))
        child=subprocess.Popen([sys.executable,'-u',str(ROOT/script),*map(str,arguments)],env=env,stdout=stream,stderr=subprocess.STDOUT)
        children[role]=child
        status({'status':'worker_started','role':role,'pid':child.pid,'log':str(directory/(role+'.log'))})
        return child
    try:
        # Owner-only resolved settings are retained for diagnosis/recovery, never
        # stored in Nix. They contain no wallet keys. No existing files are reused.
        temporary=Path(tempfile.mkdtemp(prefix='runtime-',dir=directory))
        index_config=temporary/'indexer.json';index_config.write_text(json.dumps(indexer))
        capital=read_json(args.capital_config)
        capital_config=temporary/'capital.json';capital_config.write_text(json.dumps(capital))
        indexer_attached=attached(args.onchain_db,'indexer',indexer)
        if args.external_workers and not indexer_attached:
            raise ValueError('No matching independently supervised indexer is running')
        if indexer_attached and not args.external_indexer:
            raise ValueError('Indexer already owned by another runner; use --external-workers with an independently running data service')
        if not args.external_indexer:
            spawn('indexer','run_onchain_indexer.py',['--config',index_config,'--db',args.onchain_db,'--loop'])
        else:status({'status':'existing_indexer_selected','ownership':'external_not_stopped_by_this_runner'})
        if args.mode=='indexer':
            monitor(args,children,None,status,lambda:stopping)
            return
        if args.manage_only:
            live_env=dict(os.environ,CRYPTO_BOT_PROVIDER_LIMITS=environment['CRYPTO_BOT_PROVIDER_LIMITS'],
                          SOLANA_ENV_FILE=str(args.env_file.resolve()))
            spawn('consumer','run_meme_live.py',['--state-dir',args.live_state,'--onchain-db',args.onchain_db,
                '--capital-config',capital_config,'--execute','--manage-only','--loop',*extra],live_env)
            monitor(args,children,children['consumer'],status,lambda:stopping)
            return
        learning_config=temporary/'learning.json';learning_config.write_text(json.dumps(learning))
        learner_attached=attached(args.unified_db,'learning',{'pipeline':learning,'capital':capital})
        if args.external_workers and not learner_attached:
            raise ValueError('No matching independently supervised learner is running')
        if learner_attached and not args.external_workers:
            raise ValueError('Learning worker already owned; use --external-workers to share independently supervised workers')
        if not args.external_workers:
            spawn('learning','run_unified_learning.py',['--db',args.unified_db,'--pipeline-config',learning_config,
                '--capital-config',capital_config,'--loop','--interval',args.learning_interval])
        else:status({'status':'existing_learning_worker_selected','ownership':'external_not_stopped_by_this_runner'})
        # Wait for real schemas, not just files created before SQLite setup.
        from unified_observations import ObservationStore
        from onchain_store import EventStore
        deadline=time.monotonic()+30
        while not stopping:
            output.poll()
            if any(p.poll() is not None for p in children.values()):raise RuntimeError('A data worker failed before consumer startup; inspect its owner-only log')
            try:
                EventStore(args.onchain_db,readonly=True).next_slot()
                ObservationStore(args.unified_db,create=False)
                break
            except (ValueError,OSError,sqlite3.Error):
                if time.monotonic()>=deadline:raise RuntimeError('Worker databases did not initialize within 30 seconds; inspect logs')
                time.sleep(.2)
        if stopping:return
        if args.mode=='data':
            monitor(args,children,None,status,lambda:stopping)
            return
        common=['--state-dir',state,'--onchain-db',args.onchain_db,'--capital-config',capital_config,
                '--unified-db',args.unified_db,'--unified-config',learning_config,'--loop']
        if args.mode=='paper':
            arguments=common+['--discover','--interval','30','--learning-live-db',args.live_state/'meme_live.sqlite']
            if args.initialize:arguments+=['--initialize']
            spawn('consumer','run_paper.py',arguments+extra)
        else:
            live_env=dict(os.environ,CRYPTO_BOT_PROVIDER_LIMITS=environment['CRYPTO_BOT_PROVIDER_LIMITS'],
                          SOLANA_ENV_FILE=str(args.env_file.resolve()))
            spawn('consumer','run_meme_live.py',common+['--execute','--learning-paper-db',args.paper_state/'paper.sqlite']+extra,live_env)
        monitor(args,children,children['consumer'],status,lambda:stopping)
    except (OSError,ValueError,RuntimeError) as error:
        startup_error=safe_reason(error)
        status({'status':'startup_failed','reason':safe_reason(error),'state_preserved':True})
        raise SystemExit(1) from None
    finally:
        remaining=[]
        for role in ('consumer','learning','indexer'):
            if role in children:
                try:stop(children[role])
                except (OSError,subprocess.TimeoutExpired):remaining.append(role)
        for stream in streams:stream.close()
        output.poll()
        for sig,handler in old.items():signal.signal(sig,handler)
        status({'status':'shutdown_incomplete' if remaining else 'stopped','remaining_workers':remaining,
                'startup_error':startup_error,'state_preserved':True})


def monitor(args,children,consumer,status,stopping):
    reported=set();next_health=0
    while not stopping():
        output=getattr(args,'worker_output',None)
        if output:output.poll()
        if consumer and consumer.poll() is not None:
            if consumer.returncode:raise RuntimeError('Consumer stopped with errors; inspect consumer.log; no automatic order retry')
            return
        for role,process in children.items():
            if role!='consumer' and process.poll() is not None and role not in reported:
                reported.add(role)
                status({'status':'degraded','failed_role':role,'exit_code':process.returncode,
                        'reason':'Existing-position consumer retained; no automatic worker or order retry'})
                if consumer is None:raise RuntimeError('Data worker stopped; checkpoints and models retained')
        if time.monotonic()>=next_health:
            try:
                from runtime_health import health
                report=health(args.onchain_db)
                progress=report.get('progress') or {}
                insufficient=progress.get('lag_growing') or progress.get('keeping_up') is False
                status({'status':'degraded' if reported else 'throughput_insufficient' if insufficient else 'data_ready' if report['ready'] else 'warming_up_or_data_blocked',
                        'data_ready':report['ready'],'blockers':report['blockers'],'progress':report.get('progress'),
                        'failed_workers':sorted(reported),
                        'model_selection_configured':getattr(args,'model_selection_configured',False),
                        'learning_worker_running':not ('learning' in reported),
                        'note':'Data readiness is not ML qualification or permission to trade'})
            except (ValueError,OSError,KeyError,sqlite3.Error) as error:
                status({'status':'data_unavailable','reason':safe_reason(error)})
            next_health=time.monotonic()+30
        time.sleep(.5)


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError,ConnectionError,RuntimeError,EOFError) as error:
        print(json.dumps({'status':'blocked','reason':safe_reason(error),'services_started':False}),flush=True)
        raise SystemExit(2) from None
