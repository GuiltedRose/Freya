"""Operator-run aggregate validation. Does not launch bot/indexer/trading services.

Offline tests use temporary provider accounting and block non-loopback sockets.
Native stress, kernel checks and Nix checks require explicit flags on the monolith.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parent


def offline_network():
    connect=socket.socket.connect
    resolve=socket.getaddrinfo
    def permitted(host):
        if host in (None,'localhost'):return True
        try:return ipaddress.ip_address(host).is_loopback
        except ValueError:return False
    def guarded_connect(sock,address):
        if isinstance(address,tuple) and not permitted(address[0]):
            raise RuntimeError('Validation forbids external network connections')
        return connect(sock,address)
    def guarded_resolve(host,*args,**kwargs):
        if not permitted(host):raise RuntimeError('Validation forbids external DNS lookups')
        return resolve(host,*args,**kwargs)
    socket.socket.connect=guarded_connect
    socket.getaddrinfo=guarded_resolve


def protect_state():
    def guard(event,args):
        if event!='open' or not isinstance(args[0],(str,bytes,os.PathLike)):return
        path=Path(os.fsdecode(args[0])).resolve()
        try:relative=path.relative_to(ROOT)
        except ValueError:return
        if relative.parts and relative.parts[0] in ('state','.env','models','checkpoints'):
            raise PermissionError('Validation cannot access operational state or credentials')
    sys.addaudithook(guard)


def tests(native=False,kernel=False):
    sys.path.insert(0,str(ROOT/'tests'))
    sys.dont_write_bytecode=True
    offline_network()
    protect_state()
    suite=unittest.TestSuite()
    omitted=[]
    for path in sorted((ROOT/'tests').glob('test_*.py')):
        if path.stem=='test_solana_network_kernel' and not kernel:
            omitted.append(path.stem);continue
        if path.stem=='test_native_transport_stress' and not native:
            omitted.append(path.stem);continue
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(path.stem))
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    return {'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
            'skips':[{'test':str(test),'reason':reason} for test,reason in result.skipped],
            'omitted':omitted,'passed':result.wasSuccessful(),'complete':result.wasSuccessful() and not result.skipped and not omitted}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--native',action='store_true')
    parser.add_argument('--kernel',action='store_true',help='Explicit isolated namespace tests; requires root on monolith')
    parser.add_argument('--nix',action='store_true',help='Run flake checks on operator Nix host')
    args=parser.parse_args()
    if args.output.exists():parser.error('Output must be new; prior validation evidence is preserved')
    if args.kernel and os.geteuid()!=0:parser.error('--kernel requires root on the monolith')
    # Never import the operator signing key into a test process.
    for key in ('SOLANA_PRIVATE_KEY','SOLANA_KEYPAIR_PATH','SOLANA_RPC_URL'):
        os.environ.pop(key,None)
    if args.native:os.environ['SOLANA_NATIVE_LOOPBACK_STRESS']='1'
    if args.kernel:os.environ['SOLANA_SERVER_KERNEL_TESTS']='I_AM_ON_THE_MONOLITH'
    report={'started_at':time.time(),'strategy':'momentum_reclaim','services_started':False,
            'live_ready':False,'profitability_demonstrated':False}
    with tempfile.TemporaryDirectory(prefix='crypto-bot-validation-') as directory:
        os.environ['CRYPTO_BOT_PROVIDER_LIMITS']=str(Path(directory)/'providers.sqlite')
        report['python']=tests(args.native,args.kernel)
    if args.nix:
        try:
            result=subprocess.run(['nix','flake','check','path:'+str(ROOT)],cwd=ROOT,check=False)
            report['nix']={'passed':result.returncode==0,'returncode':result.returncode}
        except OSError as error:
            report['nix']={'passed':False,'reason':type(error).__name__}
    report['completed_at']=time.time()
    report['complete']=report['python']['complete'] and report.get('nix',{}).get('passed',False)
    with args.output.open('x') as stream:
        json.dump(report,stream,indent=2,allow_nan=False)
        stream.write('\n')
    print(json.dumps({'report':str(args.output),'complete':report['complete'],'live_ready':False}))
    if not report['python']['passed'] or args.nix and not report['nix']['passed']:
        raise SystemExit(1)


if __name__=='__main__':main()
