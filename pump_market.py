"""Saved candle parsing and shared conservative paper execution assumptions.

Live Pump web collection is retired. OnchainPaperSources overrides market and SOL
valuation access while retaining the modeled execution/rent helpers below.
"""
import time
from pathlib import Path
from meme_evidence import number
from paper_sources import PublicSources
from data_policy import SourceAccessBlocked
from pump_narratives import ROOT



def pump_history(payload,mint,pool,url,now):
    if payload['served']!={'res':'1m','measure':'price_usd'}:
        raise ValueError('Pump must serve one-minute USD price candles')
    rows,seen=[],set()
    for candle in payload['candles']:
        stamp=number(candle['openTimeMs'])/1000
        if stamp in seen:
            raise ValueError('Duplicate Pump candle')
        seen.add(stamp)
        if candle['final'] is not True or stamp+60>now:
            continue
        rows.append({'timestamp':stamp,'open':number(float(candle['open']),1e-15),
                     'high':number(float(candle['high']),1e-15),'low':number(float(candle['low']),1e-15),
                     'close':number(float(candle['close']),1e-15),'volume_usd':number(float(candle['volumeUsd']))})
    return {'mint':mint,'pool':pool,'source_url':url,'retrieved_at':now,'interval_seconds':60,
            'measure':'price_usd','candles':sorted(rows,key=lambda row:row['timestamp']),
            'provider_has_older':payload['hasOlder'],'raw_candles_received':len(payload['candles']),
            'partial_candles_excluded':len(payload['candles'])-len(rows),'history_scope':'provider token history; not full chain reconstruction'}


class PumpMarketSources(PublicSources):
    def observation(self,mint,now=None,pool=None):
        raise SourceAccessBlocked('Legacy Pump web market collection was removed; use finalized on-chain data or a permitted replay feed')


class ModeledPumpSources(PumpMarketSources):
    """Shared conservative paper assumptions, never live execution evidence."""

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.sol_quote=None
        self.sol_quote_time=0

    def market_observation(self,mint,now=None,pool=None):
        return super().observation(mint,now,pool)

    def observation(self,mint,now=None,pool=None):
        row=self.market_observation(mint,now,pool)
        current=time.time() if now is None else now
        if self.sol_quote is None or current-self.sol_quote_time>60:
            quote_payload=self.fetch(ROOT+'/sol-price')
            if quote_payload.get('stale') is not False:
                raise ValueError('Pump SOL valuation stale or unavailable')
            current=time.time() if now is None else now
            quote_stamp=number(quote_payload['asOfTimestamp'])/1000
            if not 0 <= current-quote_stamp <= 120:
                raise ValueError('Pump SOL valuation expired')
            self.sol_quote=number(quote_payload['solPrice'],1e-15)
            self.sol_quote_time=current
        from meme_dip import SOL_MINT,USDC_MINT
        quote_mint=row.get('quote_mint')
        if quote_mint=='11111111111111111111111111111111':
            quote_mint=SOL_MINT
        supported=quote_mint in (SOL_MINT,USDC_MINT)
        measured_rent=.00203928
        account_prepared=False
        observation_path=Path('state/wallet_observation.json')
        try:
            import json
            observation=json.loads(observation_path.read_text())
            if 0<=current-observation['observed_at']<=86400:
                size='170' if row.get('token_program')=='TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb' else '165'
                measured_rent=number(observation['minimum_rent_lamports'][size])/1e9
                if current-observation['observed_at']<=300:
                    from solders.pubkey import Pubkey
                    program=row.get('token_program') or 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
                    expected=str(Pubkey.find_program_address([bytes(Pubkey.from_string(observation['wallet'])),bytes(Pubkey.from_string(program)),bytes(Pubkey.from_string(mint))],Pubkey.from_string('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'))[0])
                    account_prepared=any(account['mint']==mint and account['token_program']==program and account['address']==expected for account in observation['token_accounts'])
        except (OSError,ValueError,KeyError,TypeError,ImportError):
            pass
        row['execution']={'mint':mint,'pool':row['pool'],'observed_at':row['observed_at'],
                          'sol_price_usd':self.sol_quote,'quote_mint':quote_mint,
                          'network_fee_sol':.00002,'fee_per_side':.0125,
                          'fee_assumption_source':'https://pump.fun/docs/fees','entry_rent_sol':0 if account_prepared else measured_rent,'exit_rent_sol':0,
                          'rent_is_refundable':True,'rent_source':'read-only RPC snapshot when fresh, conservative fallback otherwise',
                          'buy_route_available':supported,'sell_route_available':supported,
                          'model_only':True,'assumptions':[
                              'Sampled Pump price plus configured adverse slippage and conservative 1.25% pool fees per side',
                              'Direct execution modeled; no Pump web/app platform fee assumed',
                              '20,000-lamport network allowance per side',
                              'New token-account rent is locked principal; existing verified ATA avoids setup funding',
                              'Simulated closed accounts retain rent until an explicit paper reclaim',
                              'No on-chain executable quote or wallet-account inventory verified']}
        row['safety_unknown']=True
        return row
