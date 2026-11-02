"""Approved direct-RPC quotes for existing live positions only; no entry data."""
from decimal import Decimal
import sqlite3
import time
import os
from pipeline_runtime import read_json,resolve_indexer
from solana_rpc_compat import check_boundary,close_rpc_clients,make_rpc
from provider_limits import ProviderBudget,provider_limits_path
from onchain_oracle import capture_sol,decode_sol
from wallet_watch import SOL


class RecoverySources:
    def __init__(self,sources,config,index_db):
        self.sources=sources
        self.config=resolve_indexer(read_json(config),{'SOLANA_RPC_URL':os.environ.get('SOLANA_RPC_URL','')})
        self.client=None;self.oracle_cache=None
        oracle=self.config.get('oracle',{})
        self.feed=next((feed for feed in oracle.get('feeds',[]) if feed.get('mint')==SOL),None)
        self.endpoint=next((endpoint for endpoint in self.config.get('endpoints',[])
            if endpoint.get('enabled') is True and endpoint.get('source_id')==oracle.get('source_id')),None)
        if self.feed is None or self.endpoint is None:raise ValueError('Recovery requires an approved SOL/USD oracle source')
        policy=self.endpoint.get('policy',{})
        if (policy.get('usd_conversion_verified') is not True or policy.get('oracle_data_rights_verified') is not True
            or type(policy.get('oracle_max_confidence_bps')) is not int
            or not 0<=policy['oracle_max_confidence_bps']<=10000):
            raise ValueError('Recovery retains the configured oracle policy')
        self.budget=ProviderBudget(provider_limits_path(index_db))

    def __getattr__(self,name):return getattr(self.sources,name)

    def oracle_price(self):
        now=time.time()
        if self.oracle_cache and 0<=now-self.oracle_cache['retrieved_at']<=15 and 0<=now-self.oracle_cache['publish_time']<=60:
            return self.oracle_cache
        check_boundary(self.endpoint)
        if self.client is None:self.client=make_rpc(self.endpoint,budget=self.budget)
        quote=decode_sol(capture_sol(self.client,feed=self.feed,shard=self.feed.get('shard',0)),feed=self.feed)
        if not 0<=time.time()-quote['publish_time']<=60:
            raise ValueError('Recovery requires a current oracle publication, not a replayed snapshot')
        if Decimal(quote['confidence_bps'])>self.endpoint['policy']['oracle_max_confidence_bps']:
            raise ValueError('Recovery oracle confidence exceeds source policy')
        self.oracle_cache=dict(quote,retrieved_at=time.time())
        return self.oracle_cache

    def fetch(self,url):
        try:return self.sources.fetch(url)
        except (OSError,ValueError,KeyError,sqlite3.Error):
            if not url.endswith('/sol-price'):raise
        quote=self.oracle_price()
        return {'stale':False,'solPrice':float(quote['usd']),'asOfTimestamp':quote['publish_time']*1000}

    def position_observation(self,engine,position):
        """Identity and wallet inventory are rechecked by prepare before submission."""
        units=position.get('units')
        if type(units) is not int or units<=0:raise ValueError('Recovery requires a durable held quantity')
        quote=self.oracle_price();price=float(quote['usd'])
        adapter=engine.adapter_factory(engine.rpc,engine.user,position['mint'],position['pool'],position['kind'])
        market=adapter.snapshot(for_entry=False)
        if market['base_balance']<units:raise ValueError('Held inventory needs reconciliation')
        out=adapter.output('sell',units,market)
        if out<=0 or not 0<=time.time()-market['read_at']<=30:raise ValueError('Fresh liquidation quote unavailable')
        return {'mint':position['mint'],'pool':position['pool'],'market_kind':position['kind'],
            'quote_mint':SOL,'observed_at':market['read_at'],'price_usd':out/1e9*price/(units/10**market['decimals']),
            'exit_only':True,'recovery_sol_price_usd':price,'market_verification':'direct held-position liquidation quote',
            'data_rights':{'learning_allowed':False},'data_errors':['entry market/candle data unavailable'],
            'data_provenance':{'source_id':self.endpoint['source_id'],'slot':market['slot'],
                'oracle_publish_time':quote['publish_time'],'purpose':'existing_position_management_only'}}

    def close(self):
        if self.client is not None:close_rpc_clients([self.client])
