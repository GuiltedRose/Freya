"""Shared pipeline preflight, ownership and secret-free diagnostics; no RPC I/O."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import time
from urllib.parse import urlsplit


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def paper_state_directory(explicit=None,configured=None):
    """Share defaults without moving, merging or abandoning legacy paper state."""
    if explicit or configured:return Path(explicit or configured).resolve()
    current=Path('state/paper_volume');legacy=Path('state/paper_memes')
    present=[directory for directory in (current,legacy) if (directory/'paper.sqlite').is_file()]
    if len(present)>1:
        raise ValueError('Paper sessions exist in both state/paper_volume and state/paper_memes; select --state-dir explicitly. Nothing was moved or reset')
    return (present[0] if present else current).resolve()


def read_json(path):
    with Path(path).open('rb') as stream:data=stream.read(1_000_001)
    if len(data)>1_000_000:raise ValueError('Configuration exceeds size limit')
    def nonfinite(_):raise ValueError('Nonfinite configuration value')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('Duplicate configuration key')
            result[key]=value
        return result
    document=json.loads(data,parse_constant=nonfinite,object_pairs_hook=unique)
    if not isinstance(document,dict):raise ValueError('Configuration must be a JSON object')
    return document


def check_index_target(path):
    path=Path(path)
    if not path.exists():return
    if not path.is_file() or path.is_symlink():raise ValueError('Index target must be a dedicated regular database file')
    connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:connection.close()
    if not {'blocks','ranges','sources','checkpoints'}<=tables:
        raise ValueError('Existing database is not a finalized index; no schema or state will be overwritten')


def resolve_indexer(config,env):
    """Only an explicit placeholder can take its endpoint from the environment."""
    config=json.loads(json.dumps(config));rpc=env.get('SOLANA_RPC_URL','')
    for endpoint in config.get('endpoints',[]):
        if endpoint.get('url')=='${SOLANA_RPC_URL}':
            if not rpc:raise ValueError('SOLANA_RPC_URL is required by the indexer config')
            host=urlsplit(rpc).hostname
            if host not in endpoint.get('policy',{}).get('allowed_rpc_hosts',[]):
                raise ValueError('RPC hostname is outside the source policy allowed_rpc_hosts; review the source before changing providers')
            endpoint['url']=rpc
        # Cycle rest must not become a five-second delay on every RPC call.
        # ProviderBudget still applies its unchanged hard request minimum.
        profile=endpoint.get('policy',{}).get('profile',{}).get('kind')
        if endpoint.get('source_id','').startswith('builtin-') and profile in ('public_mainnet','selfhosted'):
            endpoint.setdefault('request_interval_seconds',.5)
    # Literal configured endpoints retain their reviewed identity. The environment
    # is consumed only where the config explicitly requests it.
    return config


def preflight(indexer,learning,*,training=True,ledger_paths=()):
    """Collect independent blockers instead of failing one setting at a time."""
    from runtime_health import safe_reason,validate_config
    issues=[]
    endpoints=indexer.get('endpoints',[])
    if not isinstance(endpoints,list) or any(not isinstance(e,dict) or not isinstance(e.get('policy',{}),dict) for e in endpoints):
        return ['indexer.endpoints must contain source objects with policy objects']
    if not isinstance(indexer.get('oracle',{}),dict):return ['indexer.oracle must be an object']
    if training and learning is not None:
        if any(not isinstance(learning.get(key,{}),dict) for key in ('rights','provenance','candidates')):
            return ['learning rights, provenance and candidates must be objects']
        ledgers=learning.get('ledgers',[])
        if not isinstance(ledgers,list) or any(not isinstance(row,dict) or not isinstance(row.get('path'),str) for row in ledgers):
            return ['learning.ledgers must contain ledger objects with string paths']
    endpoints=[e for e in indexer.get('endpoints',[]) if e.get('enabled') is True]
    if not endpoints:issues.append('indexer.endpoints: enable a reviewed RPC source')
    for endpoint in endpoints:
        evidence=endpoint.get('policy',{}).get('evidence')
        if not isinstance(evidence,str) or not evidence.strip() or 'REPLACE' in evidence.upper():
            issues.append('source policy requires actual review evidence, not a template placeholder')
        for key in ('storage_verified',)+(('ml_verified','evaluation_verified','usd_conversion_verified','oracle_data_rights_verified') if training else ()):
            if endpoint.get('policy',{}).get(key) is not True:issues.append('source policy requires '+key)
    versions=indexer.get('reviewed_program_versions')
    if not versions:issues.append('indexer.reviewed_program_versions: reviewed deployment slots and matching IDL hashes are required')
    if training and not indexer.get('oracle',{}).get('feeds'):
        issues.append('indexer.oracle.feeds: configure reviewed USD feeds; the RPC URL does not provide automatic USD conversions')
    try:validate_config(indexer)
    except (ValueError,KeyError,TypeError,OSError,RuntimeError,ImportError) as error:issues.append('indexer: '+safe_reason(error))
    if training:
        if learning is None:issues.append('learning configuration is required')
        else:
            from learning_pipeline import validate_config as validate_learning
            try:validate_learning(learning)
            except (ValueError,KeyError,TypeError) as error:issues.append('learning: '+safe_reason(error))
            for section in ('rights','provenance'):
                evidence=learning.get(section,{}).get('evidence')
                if not isinstance(evidence,str) or not evidence.strip() or 'REPLACE' in evidence.upper():
                    issues.append('learning.'+section+' requires actual evidence, not a template placeholder')
            for key in ('storage_verified','ml_verified','evaluation_verified'):
                if learning.get('rights',{}).get(key) is not True:issues.append('learning.rights requires '+key)
            for key in ('verified','entry_timestamp_verified','quote_timestamp_verified','net_outcomes_verified'):
                if learning.get('provenance',{}).get(key) is not True:issues.append('learning.provenance requires '+key)
            configured={str(Path(row['path']).resolve()) for row in learning.get('ledgers',[]) if 'path' in row}
            for path in ledger_paths:
                if str(Path(path).resolve()) not in configured:issues.append('learning.ledgers must include '+str(path))
            if len(learning.get('ledgers',[]))>4:issues.append('learning.ledgers exceeds the bounded worker limit')
    return list(dict.fromkeys(issues))


@contextmanager
def writer_lock(database,kind,config):
    path=Path(str(database)+'.'+kind+'.lock')
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(descriptor,'r+') as stream:
        try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Another '+kind+' owns this database; use the coordinated runner or stop that writer') from None
        stream.seek(0);stream.truncate()
        json.dump({'pid':os.getpid(),'config':fingerprint(config),'started':time.time()},stream)
        stream.flush();os.fsync(stream.fileno())
        try:yield
        finally:fcntl.flock(stream,fcntl.LOCK_UN)


def attached(database,kind,config):
    path=Path(str(database)+'.'+kind+'.lock')
    if not path.exists():return False
    if path.is_symlink() or path.stat().st_uid!=os.getuid() or path.stat().st_mode & 0o022:
        raise ValueError('Untrusted worker ownership file')
    with path.open() as stream:
        try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            metadata=json.loads(stream.read(4096))
            if metadata.get('config')!=fingerprint(config):raise ValueError('Running '+kind+' uses different configuration; explicit reconciliation required')
            return True
        else:fcntl.flock(stream,fcntl.LOCK_UN)
    return False
