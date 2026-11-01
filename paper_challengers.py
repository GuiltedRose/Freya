"""Independently configured PAPER-ONLY management experiments.

These policies cannot supply an entry signal or authorize live transactions.
"""
from dataclasses import dataclass,asdict
import math
from market_analysis import closed_bars,lifecycle


@dataclass(frozen=True)
class ChallengerConfig:
    adaptive_target: bool = False
    partial_profits: bool = False
    trailing_exit: bool = False
    adaptive_holding: bool = False
    scaling: bool = False
    reentry: bool = False
    portfolio_allocation: bool = False
    target_min: float = .1
    target_max: float = .6
    target_volatility_multiple: float = 3.
    partial_milestones: tuple = (.15,.3,.5)
    partial_fractions: tuple = (.25,.25,.5)
    trailing_giveback: float = .3
    holding_min: int = 300
    holding_max: int = 1800
    scale_fraction: float = .2
    max_additions: int = 1
    reentry_cooldown: int = 300
    churn_window: int = 3600
    max_entries_per_window: int = 3
    portfolio_fraction: float = .5

    def __post_init__(self):
        for key in ('adaptive_target','partial_profits','trailing_exit','adaptive_holding','scaling','reentry','portfolio_allocation'):
            if type(getattr(self,key)) is not bool:raise ValueError('Challenger flags must be boolean')
        numbers=(self.target_min,self.target_max,self.target_volatility_multiple,self.trailing_giveback,self.scale_fraction,self.portfolio_fraction)
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in numbers):raise ValueError('Invalid challenger number')
        if not 0<self.target_min<=self.target_max<=2 or not 0<self.target_volatility_multiple<=10:raise ValueError('Invalid target bounds')
        if not 0<self.trailing_giveback<1 or not 0<self.scale_fraction<=.5 or not 0<self.portfolio_fraction<=1:raise ValueError('Invalid allocation/exit bounds')
        if not 60<=self.holding_min<=self.holding_max<=3600 or not 0<=self.max_additions<=3:raise ValueError('Invalid holding/scaling bounds')
        if not 60<=self.reentry_cooldown<=86400 or not 60<=self.churn_window<=86400 or not 1<=self.max_entries_per_window<=20:raise ValueError('Invalid churn bounds')
        if len(self.partial_milestones)!=len(self.partial_fractions) or not 1<=len(self.partial_milestones)<=8:raise ValueError('Invalid milestone dimensions')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (*self.partial_milestones,*self.partial_fractions)):raise ValueError('Nonfinite partial-profit configuration')
        if list(self.partial_milestones)!=sorted(set(self.partial_milestones)) or any(not 0<v<=2 for v in self.partial_milestones) or any(not 0<f<=1 for f in self.partial_fractions):raise ValueError('Invalid partial-profit milestones')


class PaperManagement:
    def __init__(self,config):self.config=config

    def holding_seconds(self,position,snapshot,baseline):
        if not self.config.adaptive_holding:return baseline
        regime=lifecycle(snapshot)['regime']
        proposed=self.config.holding_min if regime in ('unstable','consolidation','unknown') else self.config.holding_max
        # Fix the limit at first management observation; never extend it repeatedly.
        return position.setdefault('challenger_holding_seconds',proposed)

    def target(self,position,snapshot,baseline):
        if not self.config.adaptive_target:return baseline
        bars=closed_bars(snapshot)
        if not bars:return baseline
        volatility=sum((b['high']-b['low'])/b['close'] for b in bars)/len(bars)
        fraction=min(self.config.target_max,max(self.config.target_min,volatility*self.config.target_volatility_multiple))
        return position['cash_spent']*fraction

    def exit_plan(self,position,snapshot,net,emergency=False):
        cfg=self.config
        if emergency:return {'fraction':1.,'reason':'emergency'}
        total_net=net+position.get('realized_parts_pnl',0)
        peak=position['challenger_peak_net']=max(position.get('challenger_peak_net',0.),total_net)
        if cfg.trailing_exit and peak>0 and total_net<=peak*(1-cfg.trailing_giveback):
            return {'fraction':1.,'reason':'challenger trailing exit'}
        if cfg.partial_profits and net>0:
            index=position.get('partial_stage',0)
            if index<len(cfg.partial_milestones) and net/max(position['cash_spent'],1e-12)>=cfg.partial_milestones[index]:
                return {'fraction':cfg.partial_fractions[index],'reason':'challenger partial profit','stage':index+1}
        return {'fraction':0.,'suppress_target':cfg.partial_profits and position.get('partial_stage',0)>0}

    def entry_allowed(self,trader,snapshot):
        if not self.config.reentry:return True
        history=getattr(trader,'challenger_history',{})
        data=history.get(snapshot['mint'],{})
        now=snapshot['observed_at']
        entries=[t for t in data.get('entries',[]) if 0<=now-t<self.config.churn_window]
        return (not data.get('closed_at') or now-data['closed_at']>=self.config.reentry_cooldown) and len(entries)<self.config.max_entries_per_window

    def entered(self,trader,snapshot):
        history=getattr(trader,'challenger_history',{})
        data=history.setdefault(snapshot['mint'],{})
        data['entries']=[t for t in data.get('entries',[]) if snapshot['observed_at']-t<self.config.churn_window]+[snapshot['observed_at']]
        trader.challenger_history=history

    def closed(self,trader,snapshot):
        history=getattr(trader,'challenger_history',{})
        history.setdefault(snapshot['mint'],{})['closed_at']=snapshot['observed_at']
        trader.challenger_history=history

    def allocation(self,trader,snapshot):
        if not self.config.portfolio_allocation:return 1.
        bars=closed_bars(snapshot)
        if not bars:return 0.
        volatility=sum((b['high']-b['low'])/b['close'] for b in bars)/len(bars)
        # A reduction only; the Kelly controller and shared exposure cap still run.
        return min(self.config.portfolio_fraction,1/(1+10*volatility))/max(1,len(trader.all_positions())+1)

    def scale(self,trader,position,snapshot,net):
        cfg=self.config
        if not cfg.scaling or net<=0 or position.get('scale_count',0)>=cfg.max_additions:return None
        from volume_candles import assess_volume_candles
        signal=assess_volume_candles(snapshot,snapshot['observed_at'],trader.rules)
        bars=closed_bars(snapshot)
        if signal['action']!='buy' or not bars:return None
        bar=bars[-1]['timestamp']
        if bar<=position.get('last_scale_bar',position['opened_at']//60*60):return None
        policy=getattr(trader,'capital_policy',None)
        if not policy or trader.halted or getattr(trader,'capital_error',None) or snapshot['observed_at']<trader.loss_cooldown_until:return None
        execution=snapshot.get('execution',{})
        price=snapshot['price_usd'];sol=trader.sol_price_usd
        if trader.native_sol is None or not sol or execution.get('quote_mint')!='So11111111111111111111111111111111111111112':return None
        if execution.get('buy_route_available') is not True:return None
        network=execution['network_fee_sol'];fee=execution.get('fee_per_side',trader.rules.fee_per_side)
        equity=trader.equity(position['quantity']*price)
        book=trader.all_positions()
        if any(not 0<=snapshot['observed_at']-p.get('last_quote_at',snapshot['observed_at'])<=30 for p in book):return None
        available=max(0,(trader.native_sol-trader.rules.native_fee_reserve_sol-2*network)*sol-trader.rules.reserve_usd)
        limit=min(available,max(0,trader.rules.position_cap(equity)-position['cash_spent']),position['cash_spent']*cfg.scale_fraction)
        exposure=sum(max(p['cash_spent'],p['quantity']*p.get('last_price_usd',p['entry_price']))+p.get('capital_reserved_usd',0) for p in book)
        own=position['cash_spent']+position.get('capital_reserved_usd',0)
        sized=policy.size(equity,limit,exposure,max(0,len(book)-1),existing_position_usd=own,
            ml_fraction=getattr(trader,'allocation_fraction',1.),overhead_usd=network*2*sol,
            liquidity_limit=max(0,snapshot.get('liquidity_usd',0)*trader.rules.max_liquidity_allocation_fraction-own),now=snapshot['observed_at'])
        from execution_liquidity import cap_notional
        budget=cap_notional(sized['allocation_usd'],snapshot.get('liquidity_usd',0),
            slippage=trader.rules.slippage_per_side,fee_fraction=fee,fixed_cost=network*sol)['maximum_usd']
        if budget<=network*2*sol or trader.native_sol<trader.rules.native_fee_reserve_sol+2*network+budget/sol:return None
        quantity=budget/(price*(1+trader.rules.slippage_per_side)*(1+fee))
        trader.native_sol-=budget/sol+network
        original_cost=position.get('total_cost_usd',position['cash_spent']+position.get('entry_native_cost_usd',0))
        position['quantity']+=quantity;position['cash_spent']+=budget
        position['entry_native_cost_usd']=position.get('entry_native_cost_usd',0)+network*sol
        position['total_cost_usd']=original_cost+budget+network*sol
        position['capital_reserved_usd']=position.get('capital_reserved_usd',0)+2*network*sol
        position['scale_count']=position.get('scale_count',0)+1;position['last_scale_bar']=bar
        return {'action':'paper_scale','mint':snapshot['mint'],'budget_usd':budget,'quantity':quantity,
                'entry_native_cost_usd':network*sol,'sizing':sized,'model_only':True}


def config_dict(config):return asdict(config)
