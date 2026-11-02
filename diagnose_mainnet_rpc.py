"""Opt-in bounded read-only mainnet comparison; never opens a signer or bot journal."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import signal
import http.client
from contextlib import contextmanager
from unittest.mock import patch


class ConnectTimeout(TimeoutError):
    connect_timeout=True


@contextmanager
def matched_python_timeouts():
    """Probe-only 15s DNS/TCP/TLS cap and 30s overall cap, including body reads.

    Both urllib connect methods are wrapped because HTTPS delegates TCP setup to
    HTTPConnection.connect. Nesting retains one common connection deadline.
    The probe worker is single-threaded and Linux-only; production is untouched.
    """
    if signal.getitimer(signal.ITIMER_REAL)!=(0.0,0.0):
        raise RuntimeError('Probe requires exclusive deadline timer')
    started=time.monotonic();depth=0;connect_started=None
    old_handler=signal.getsignal(signal.SIGALRM)
    def alarm(*_):
        raise (ConnectTimeout('RPC connect deadline') if depth else TimeoutError('RPC request deadline'))
    def wrap(original):
        def connect(connection):
            nonlocal depth,connect_started
            outer=depth==0
            if outer:connect_started=time.monotonic()
            depth+=1
            remaining=max(.001,min(15-(time.monotonic()-connect_started),30-(time.monotonic()-started)))
            signal.setitimer(signal.ITIMER_REAL,remaining)
            connection.timeout=remaining
            try:return original(connection)
            except TimeoutError:raise ConnectTimeout('RPC connect deadline') from None
            finally:
                depth-=1
                if outer:
                    remaining=max(.001,30-(time.monotonic()-started))
                    signal.setitimer(signal.ITIMER_REAL,remaining)
                    if connection.sock is not None:connection.sock.settimeout(remaining)
        return connect
    signal.signal(signal.SIGALRM,alarm)
    signal.setitimer(signal.ITIMER_REAL,30)
    try:
        with patch.object(http.client.HTTPConnection,'connect',wrap(http.client.HTTPConnection.connect)), \
             patch.object(http.client.HTTPSConnection,'connect',wrap(http.client.HTTPSConnection.connect)):
            yield
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        signal.signal(signal.SIGALRM,old_handler)


def worker(args):
    from pipeline_runtime import read_json,resolve_indexer,writer_lock
    from runtime_health import validate_config
    from provider_limits import ProviderBudget,provider_limits_path,ProviderBackoff
    from solana_rpc_compat import make_rpc,check_boundary
    from rpc_diagnostics import details
    from wallet_watch import MAINNET
    config=resolve_indexer(read_json(args.config),{'SOLANA_RPC_URL':os.environ.get('SOLANA_RPC_URL','')})
    decoder=validate_config(config)
    endpoints=[row for row in config['endpoints'] if row.get('enabled') is True]
    if len(endpoints)!=1:raise ValueError('Select exactly one reviewed endpoint')
    endpoint=endpoints[0]
    # Enforce the same admission boundary for both implementations.
    check_boundary(endpoint)
    budget=ProviderBudget(provider_limits_path(args.index_db))
    attempts=0;anchor=None;slots=None
    with writer_lock(args.index_db,'indexer',config):
        for backend in ('python','rust'):
            rpc=make_rpc(dict(endpoint,backend=backend),budget=budget)
            try:
                methods=['getGenesisHash','getSlot','getBlocks','getBlock','getBlock']
                for method in methods:
                    if attempts>=args.max_requests:return
                    params=[]
                    if method=='getBlocks':
                        if anchor is None:break
                        params=[max(0,anchor-8),anchor,{'commitment':'finalized'}]
                    elif method=='getBlock':
                        if not slots:break
                        # Explicit block cursor, identical slots for both backends.
                        slot=slots[block_cursor];block_cursor+=1
                        params=[slot,{'commitment':'finalized','encoding':'json','transactionDetails':'full',
                                      'maxSupportedTransactionVersion':1,'rewards':False}]
                    elif method=='getSlot':params=[{'commitment':'finalized'}]
                    while True:
                        try:
                            remaining=budget.cooldown_remaining(rpc.url)
                            if remaining:time.sleep(min(remaining,60));continue
                            check_boundary(endpoint)
                            before=rpc.wire_bytes;started=time.monotonic()
                            attempts+=1
                            from contextlib import nullcontext
                            with matched_python_timeouts() if backend=='python' else nullcontext():
                                value=rpc.call(method,params)
                            row={'backend':backend,'method':method,'status':'success','http_status':getattr(rpc,'last_http_status',None),
                                 'elapsed_ms':(time.monotonic()-started)*1000,'bytes_received':rpc.wire_bytes-before}
                            if method=='getGenesisHash' and value!=MAINNET:raise ValueError('Wrong chain')
                            if method=='getSlot' and anchor is None:anchor=value
                            if method=='getBlocks':
                                if slots is None:slots=value[-2:]
                                if len(slots)!=2 or any(slot not in value for slot in slots):raise ValueError('Common finalized blocks unavailable')
                                block_cursor=0
                            if method=='getBlock':
                                events=issues=0
                                for index,tx in enumerate(value['transactions']):
                                    found,problems=decoder.transaction(tx,slot,index,value.get('blockTime'))
                                    events+=len(found);issues+=len(problems)
                                row.update(slot=slot,transactions=len(value['transactions']),decoded_events=events,
                                           decoder_issues=issues,decoder_status='issues' if issues else 'decoded',
                                           source_admission='not promoted; probe does not update ingestion state')
                            print(json.dumps(row,allow_nan=False),flush=True)
                            break
                        except ProviderBackoff as error:
                            # A denied reservation did not make a network attempt.
                            attempts-=1
                            time.sleep(min(60,max(1,error.seconds)))
                        except Exception as error:
                            row={'backend':backend,'method':method,'status':'failed',
                                 'http_status':getattr(error,'http_status',None),
                                 'error_type':type(error).__name__,'category':getattr(error,'category',None),**details(error)}
                            print(json.dumps(row,allow_nan=False),flush=True)
                            # Prerequisite failure cannot be replaced with invented slots/chain identity.
                            if method in ('getGenesisHash','getSlot','getBlocks'):break
                            break
                    if row.get('status')=='failed' and method in ('getGenesisHash','getSlot','getBlocks'):break
            finally:
                close=getattr(rpc,'close',None)
                if close:close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--index-db',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--max-requests',type=int,default=10)
    parser.add_argument('--deadline-seconds',type=int,default=180)
    parser.add_argument('--allow-mainnet',action='store_true',required=True)
    parser.add_argument('--workers-stopped',action='store_true',required=True)
    parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if not 10<=args.max_requests<=20 or not 30<=args.deadline_seconds<=300:parser.error('Bounds: 10..20 requests, 30..300 seconds')
    if sys.platform!='linux':parser.error('Probe requires Linux/Monolith')
    if args.worker:
        try:worker(args)
        except Exception as error:print(json.dumps({'status':'preflight_failed','error_type':type(error).__name__}),flush=True)
        return
    # No signer environment or .env file enters the worker. Config must contain the
    # reviewed URL or the operator must export only SOLANA_RPC_URL beforehand.
    allowed=('PATH','PYTHONPATH','SSL_CERT_FILE','NIX_SSL_CERT_FILE','SOLANA_RPC_URL','CRYPTO_BOT_PROVIDER_LIMITS')
    env={key:os.environ[key] for key in allowed if key in os.environ}
    env['PYTHONDONTWRITEBYTECODE']='1'
    from provider_limits import provider_limits_path
    env['CRYPTO_BOT_PROVIDER_LIMITS']=str(provider_limits_path(args.index_db))
    fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as report:
        timed_out=False
        try:
            result=subprocess.run([sys.executable,__file__,*sys.argv[1:],'--worker'],env=env,
                                  stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,timeout=args.deadline_seconds)
            output=result.stdout
        except subprocess.TimeoutExpired as error:
            timed_out=True;output=error.stdout or b''
            if isinstance(output,bytes):output=output.decode('utf8',errors='replace')
        rows=[]
        for line in output.splitlines():
            try:rows.append(json.loads(line))
            except ValueError:pass
        complete=(not timed_out and len(rows)==10 and all(row.get('status')=='success' and not row.get('decoder_issues',0) for row in rows))
        data={'diagnostic_complete':complete,'scope':'bounded mainnet transport/receipt diagnostic; not production acceptance',
              'deadline_reached':timed_out,'maximum_logical_requests':args.max_requests,'results':rows,
              'live_ready':False,'inflight_request_unknown':timed_out}
        json.dump(data,report,indent=2,allow_nan=False);report.flush();os.fsync(report.fileno())
    print(json.dumps({'report':str(args.output),'records':len(rows),'deadline_reached':timed_out,'diagnostic_complete':complete}))
    if not complete:raise SystemExit(1)


if __name__=='__main__':main()
