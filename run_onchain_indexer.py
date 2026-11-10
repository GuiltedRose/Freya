"""Read-only finalized Pump indexer and local market shadow report."""
from solana_rpc_compat import make_rpc, close_rpc_clients
import argparse
import json
from pathlib import Path
from onchain_store import EventStore
from onchain_ingest import IndexRpc, Ingester, follow
from onchain_market import OnchainMarket
from sync_policy import initialize_store


def bootstrap_start(store,clients,requested=None):
    """Choose once for an empty index; never skip an existing durable prefix."""
    return initialize_store(store, requested)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('onchain_pipeline.json'),help='Reviewed runtime config; examples are never activated implicitly')
    parser.add_argument('--env-file',type=Path,default=Path('.env'),help='Only public collection settings are read; signing fields are ignored')
    parser.add_argument('--db', type=Path, default=Path('state/onchain/index.sqlite'))
    parser.add_argument('--start-slot', type=int)
    parser.add_argument('--batch-slots', type=int, default=8)
    parser.add_argument('--max-catchup-slots', type=int, default=64)
    parser.add_argument('--stream', action='store_true', help='Authorized WSS hints plus existing finalized recovery and local candles')
    parser.add_argument('--loop', action='store_true')
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--capture-sol-usd', action='store_true', help='Explicit reviewed Pyth account capture into the selected isolated index; no historical backfill')
    parser.add_argument('--retain-blocks', action='store_true', help='Keep compressed full blocks for independent offline replay; substantial disk cost')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--mint')
    parser.add_argument('--pool')
    args = parser.parse_args()
    if args.start_slot is not None and args.start_slot<0:parser.error('Start slot must be nonnegative')
    if not 1<=args.batch_slots<=args.max_catchup_slots<=64:parser.error('Slot batch/catch-up bounds must be 1..64')
    if not 1<=args.interval<=3600:parser.error('Indexer interval must be 1..3600 seconds')
    if not (args.status or args.mint):
        config = json.loads(args.config.read_text())
        from pipeline_runtime import resolve_indexer
        public={}
        if any(e.get('url')=='${SOLANA_RPC_URL}' for e in config.get('endpoints',[])):
            from config import public_env
            public=public_env(args.env_file)
        config=resolve_indexer(config,public)
        from runtime_health import validate_config
        decoder = validate_config(config)
        from pipeline_runtime import writer_lock,check_index_target
        check_index_target(args.db)
        with writer_lock(args.db,'indexer',config):
            return run(args,config,decoder,parser)
    return run(args,None,None,parser)


def run(args,config,decoder,parser):
    store = EventStore(args.db, readonly=bool(args.status or args.mint), retain_blocks=args.retain_blocks)
    if args.status or args.mint:
        watermark = store.watermark()
        coverage = store.coverage(watermark-3600, watermark) if watermark is not None else {
            'complete': False, 'coverage_pct': 0, 'reason': 'no finalized blocks indexed'}
        result = {'mode': 'read_only_shadow', 'metrics': store.metrics(),
                  'finalized_hour_coverage': coverage, 'progress': store.progress()}
        if args.mint:
            try:
                result['market'] = OnchainMarket(store).observation(args.mint, pool=args.pool)
            except ValueError as error:
                result['market_unavailable'] = str(error)
        print(json.dumps(result, allow_nan=False))
        return
    endpoints = []
    for endpoint in config['endpoints']:
        if endpoint.get('enabled') is not True:
            continue
        store.register_source(endpoint['source_id'], endpoint['policy'])
        endpoints.append(make_rpc(endpoint))
        from rpc_run_evidence import effective_settings
        from provider_limits import ProviderBudget
        rpc=endpoints[-1]
        if isinstance(getattr(rpc,'budget',None),ProviderBudget):
            rpc.endpoint=endpoint
            print(json.dumps({'status':'rpc_effective_settings','at':__import__('time').time(),
                              'settings':effective_settings(rpc,rpc.budget.identity(rpc.url))}),flush=True)
    try:
        if args.capture_sol_usd:
            if not endpoints:
                parser.error('A reviewed enabled RPC endpoint is required')
            from onchain_oracle import capture_sol, import_sol
            print(json.dumps(import_sol(store, capture_sol(endpoints[0]))))
            return
        ingester = Ingester(store, endpoints, config.get('reviewed_program_versions'),
                            decoder=decoder, oracle=config.get('oracle'),
                            native_handoff=config.get('native_handoff'))
        start = bootstrap_start(store,endpoints,args.start_slot)
        candles=None
        markets=config.get('markets',[])
        if len(markets)>32:parser.error('At most 32 explicitly configured candle markets')
        if markets:
            from onchain_candles import CandleEngine
            candles=CandleEngine(store)
            for market in markets:candles.watch(market['mint'],market['pool'])
        if args.stream:
            if not args.loop or not candles:parser.error('--stream requires --loop and configured markets')
            import asyncio
            from onchain_stream import run_stream
            endpoint=next(e for e in config['endpoints'] if e.get('enabled') is True)
            if store.policy(endpoint['source_id']).get('stream_verified') is not True:
                parser.error('Explicit reviewed stream policy required; no stream started')
            asyncio.run(run_stream(ingester,endpoint,start,candles,args.batch_slots,args.interval))
            return
        if args.loop:
            for result in follow(ingester, start, args.batch_slots, args.interval, max_catchup_slots=args.max_catchup_slots):
                if candles:result['candles']=candles.sync()
                print(json.dumps(result), flush=True)
        else:
            result=ingester.step(start,args.batch_slots)
            if candles:result['candles']=candles.sync()
            print(json.dumps(result))
    finally:
        close_rpc_clients(endpoints)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped. Index checkpoint preserved; no transactions submitted.')
