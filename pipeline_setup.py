"""Operator-invoked setup. Read-only RPC probes; never starts trading or indexing."""
import json
import os
from pathlib import Path
import tempfile
import uuid
import sys
from decimal import Decimal
from urllib.parse import urlsplit
from pipeline_runtime import fingerprint,preflight,read_json,resolve_indexer,writer_lock


def answer(prompt,default=None):
    value=input(prompt+(' ['+str(default)+']' if default is not None else '')+': ').strip()
    value=value or (str(default) if default is not None else '')
    if not value or 'REPLACE' in value.upper():raise ValueError('Explicit nonplaceholder setup evidence required')
    return value


def confirm(prompt):
    if input(prompt+' Type yes to confirm: ').strip().lower()!='yes':
        raise ValueError('Setup cancelled; no configuration or trading state changed')


def save_documents(documents,originals):
    """Stage complete files, retain exact backups, reject concurrent edits."""
    staged=[];backups=[]
    try:
        for path,document in documents:
            path=Path(path)
            if path.is_symlink() or (path.exists() and not path.is_file()):raise ValueError('Configuration target must be a regular file')
            current=path.read_bytes() if path.exists() else None
            if current!=originals[path]:raise ValueError('Configuration changed during setup; no replacement performed')
            path.parent.mkdir(parents=True,exist_ok=True)
            descriptor,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
            staged.append((Path(name),path))
            with os.fdopen(descriptor,'w') as stream:
                json.dump(document,stream,indent=2,allow_nan=False);stream.write('\n')
                stream.flush();os.fsync(stream.fileno())
            if current is not None:
                backup=path.with_name(path.name+'.backup-'+uuid.uuid4().hex)
                fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as stream:
                    stream.write(current);stream.flush();os.fsync(stream.fileno())
                backups.append(str(backup))
        for temporary,path in staged:os.replace(temporary,path)
        for parent in {path.parent for _,path in staged}:
            descriptor=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
    finally:
        for temporary,_ in staged:
            if temporary.exists():temporary.unlink()
    return backups


def configure(args,public,indexer,learning):
    """Gather operator evidence and facts separately; credentials stay unexported."""
    if not sys.stdin.isatty():raise ValueError('--configure requires an interactive terminal; existing JSON configs remain supported for unattended deployment')
    paths=[Path(args.indexer_config),Path(args.learning_config)]
    if paths[0]==paths[1]:raise ValueError('Indexer and learning configurations need separate paths')
    originals={}
    for path in paths:
        if path.is_symlink() or (path.exists() and not path.is_file()):raise ValueError('Untrusted configuration target')
        originals[path]=path.read_bytes() if path.exists() else None
        if path.exists():
            # Preserve operator choices rather than replacing them with defaults.
            if path==paths[0]:indexer=read_json(path)
            else:learning=read_json(path)
    source=indexer['endpoints'][0]
    if len(indexer['endpoints'])!=1 or source.get('network_mode')!='remote_provider':
        raise ValueError('Guided setup supports one remote HTTPS provider; existing isolated/home-node policies must use their dedicated setup')
    host=urlsplit(public.get('SOLANA_RPC_URL','')).hostname
    if source.get('url')=='${SOLANA_RPC_URL}':
        if not host:raise ValueError('Set SOLANA_RPC_URL in the owner-only .env first')
        source['policy']['allowed_rpc_hosts']=[host]
    print('Setup starts no services and never reads signing fields. Existing configs are backed up before replacement.')
    confirm('Authorize read-only mainnet RPC probes using the configured provider and its existing request limits?')
    evidence=answer('Source permission review evidence/reference for storage, training, evaluation and oracle use (no secrets)')
    confirm('Have you verified those uses and the remote HTTPS provider boundary are permitted?')
    policy=source['policy']
    policy.update(evidence=evidence,storage_verified=True,ml_verified=True,evaluation_verified=True,
                  usd_conversion_verified=True,oracle_data_rights_verified=True,remote_provider_verified=True)
    if args.mode=='live' and policy.get('execution_verified') is not True:
        execution=answer('Execution source/receipt validation review reference (no secrets)')
        confirm('Have you reviewed source suitability for real execution? This does not authorize a trade.')
        policy.update(execution_verified=True,execution_evidence=execution)
    source['enabled']=True
    confidence=int(answer('Maximum oracle confidence width in basis points (100 = 1%)'))
    if not 0<=confidence<=10000:raise ValueError('Oracle confidence must be 0..10000 bps')
    policy['oracle_max_confidence_bps']=confidence
    oracle=indexer['oracle'];oracle['source_id']=source['source_id']
    feed=oracle['feeds'][0]
    from wallet_watch import SOL
    if feed.get('mint')!=SOL:raise ValueError('Guided setup requires an identity-bound SOL/USD feed')
    feed['feed_id']=answer('Reviewed SOL/USD feed ID',feed['feed_id'])
    feed['shard']=int(answer('Oracle shard',feed.get('shard',0)))
    feed['evidence']=answer('Oracle feed/mint/USD identity review reference (no secrets)')
    confirm('Have you reviewed this SOL/USD feed identity, denomination and source rights?')
    feed['reviewed']=True
    if len(oracle['feeds'])!=1:raise ValueError('Guided setup handles one SOL/USD feed; retain additional feed configuration through explicit JSON review')
    indexer=resolve_indexer(indexer,public);source=indexer['endpoints'][0]
    from solana_rpc_compat import check_boundary,make_rpc,close_rpc_clients
    from onchain_decode import Decoder
    from onchain_ingest import read_program_deployments
    from onchain_oracle import capture_sol,decode_sol
    check_boundary(source)
    decoder=Decoder(backend=source.get('event_backend','python'),schema_directory=indexer.get('schema_directory'))
    rpc=make_rpc(source)
    try:
        print('Checking finalized mainnet deployments and the oracle through paced read-only RPC...',flush=True)
        rpc.validate()
        head=rpc.call('getSlot',[{'commitment':'finalized'}])
        if type(head) is not int or not 0<=head<2**63:raise ValueError('Invalid finalized slot')
        deployments=read_program_deployments(rpc,decoder.specs)
        quote=decode_sol(capture_sol(rpc,feed=feed,shard=feed['shard']),feed=feed)
        if Decimal(quote['confidence_bps'])>confidence:raise ValueError('Current oracle confidence exceeds selected policy')
        print(json.dumps({'finalized_slot':head,'deployments':deployments,
                          'idl_hashes':{program:spec['hash'] for program,spec in decoder.specs.items()}}))
        compatibility=answer('Evidence/reference confirming these deployed programs match these pinned IDLs')
        confirm('Have you verified that compatibility? RPC deployment discovery alone does not prove it.')
        if args.onchain_db.exists():
            from onchain_store import EventStore
            cursor=EventStore(args.onchain_db,readonly=True).next_slot()
            if cursor is not None and cursor<head:
                raise ValueError('Existing index checkpoint needs historical deployment ranges; guided forward-only setup will not skip or reset it. Use a separate --onchain-db or retain reviewed historical ranges')
        indexer['reviewed_program_versions']={program:[dict(from_slot=head,through_slot=2**63-1,
            deployment_slot=slot,idl_hash=decoder.specs[program]['hash'],evidence=compatibility)]
            for program,slot in deployments.items()}
        if any(slot>head for slot in deployments.values()):raise ValueError('Program deployment changed after finalized head; retry setup')
        if deployments!=read_program_deployments(rpc,decoder.specs):raise ValueError('Program upgraded during setup; no config saved')
    finally:close_rpc_clients([rpc])
    proof=answer('Learning provenance review reference for timestamped, cost-adjusted paper/live ledger records')
    confirm('Have you verified entry/quote timestamps and realized net-outcome handling? No synthetic labels or model qualification will be created.')
    learning['rights'].update(storage_verified=True,ml_verified=True,evaluation_verified=True,evidence=evidence)
    learning['provenance'].update(verified=True,entry_timestamp_verified=True,quote_timestamp_verified=True,
                                  net_outcomes_verified=True,evidence=proof)
    learning['ledgers']=[{'source':'bot_paper','path':str(args.paper_state/'paper.sqlite')},
                         {'source':'bot_live','path':str(args.live_state/'meme_live.sqlite')}]
    issues=preflight(indexer,learning,ledger_paths=[row['path'] for row in learning['ledgers']])
    if issues:raise ValueError('Setup validation failed: '+'; '.join(issues))
    confirm('Save both configurations? This authorizes no trading and starts no services.')
    with writer_lock(paths[0],'setup',{'files':[str(path) for path in paths]}):
        backups=save_documents(list(zip(paths,[indexer,learning])),originals)
    print(json.dumps({'status':'configured','mode':args.mode,'services_started':False,'runtime_ready':False,
        'configuration':[str(path) for path in paths],'backups':backups,
        'next_step':'Run --preflight, then --initialize for new paper state or --execute for live',
        'review_fingerprint':fingerprint([indexer,learning])}))
