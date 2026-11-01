"""Conservative exit-depth and price-impact caps beneath existing capital ceilings."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class LiquidityLimits:
    max_entry_impact: float = .001
    max_exit_impact: float = .005
    max_slippage: float = .005
    max_depth_fraction: float = .001

    def __post_init__(self):
        if not all(0<x<=.005 for x in (self.max_entry_impact,self.max_exit_impact,self.max_slippage,self.max_depth_fraction)):
            raise ValueError('Liquidity settings cannot exceed existing hard slippage/impact protections')


def cap_notional(proposed,exit_quote_usd,*,slippage=.005,fee_fraction=.0125,fixed_cost=0.,limits=None):
    limits=limits or LiquidityLimits()
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in
           (proposed,exit_quote_usd,slippage,fee_fraction,fixed_cost)):
        raise ValueError('Finite liquidity and execution costs required')
    if slippage>limits.max_slippage:return {'maximum_usd':0.,'reason':'slippage exceeds hard limit'}
    depth_cap=exit_quote_usd*min(limits.max_depth_fraction,limits.max_exit_impact/(1-limits.max_exit_impact))
    maximum=min(proposed,depth_cap)
    cost=maximum*(2*fee_fraction+2*slippage)+2*fixed_cost
    return {'maximum_usd':maximum,'round_trip_cost_usd':cost,'exit_quote_depth_usd':exit_quote_usd,
            'reason':'exit depth, impact and cost bound','modeled':True}


def cap_native(adapter,market,proposed,slippage=.005,limits=None):
    limits=limits or LiquidityLimits()
    if slippage>limits.max_slippage:raise ValueError('Slippage exceeds hard limit')
    lo,hi=0,min(proposed,int(market['quote']*limits.max_entry_impact))
    while lo<hi:
        mid=(lo+hi+1)//2
        try:
            tokens=adapter.output('buy',mid,market)
            output=adapter.output('sell',tokens,market)
            ideal=tokens*market['quote']/market['base']
            fee=market['fee_bps']/10000
            impact=1-output/max(1,ideal*(1-fee))
            valid=tokens>0 and output>0 and impact<=limits.max_exit_impact
        except ValueError:valid=False
        if valid:lo=mid
        else:hi=mid-1
    return lo
