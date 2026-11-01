"""Paired, group-level net-return comparisons against unchanged reclaim outcomes."""
import math
import random


def compare(rows,min_groups=30,bootstrap_samples=200):
    chosen={};ids=set()
    for row in sorted(rows,key=lambda r:(r['exit_at'],r['id'])):
        if row['id'] in ids:continue
        ids.add(row['id'])
        if not math.isfinite(row['pnl']) or not 0<=row.get('fraction',1)<=1:raise ValueError('Invalid comparative outcome')
        chosen.setdefault(row['group_id'],row)
    rows=list(chosen.values())
    baseline=[r['pnl'] for r in rows]
    selected=[r.get('policy_pnl',r['pnl']*r.get('fraction',1) if r['selected'] else 0.) for r in rows]
    differences=[b-a for a,b in zip(baseline,selected)]
    def drawdown(values):
        equity=peak=worst=0.
        for value in values:
            equity+=value;peak=max(peak,equity);worst=max(worst,peak-equity)
        return worst
    rng=random.Random(42)
    samples=sorted(sum(rng.choices(differences,k=len(rows)))/len(rows) for _ in range(bootstrap_samples)) if rows else []
    lower=samples[int(.025*len(samples))] if samples else None
    uncertainty=[]
    brier=[]
    for low in (0,.2,.4,.6,.8):
        cohort=[r for r in rows if type(r.get('probability')) in (int,float) and low<=r['probability']<=1 and (r['probability']<low+.2 or low==.8)]
        if cohort:
            observed=sum(r['pnl']>0 for r in cohort)/len(cohort)
            predicted=sum(r['probability'] for r in cohort)/len(cohort)
            uncertainty.append({'lower':low,'count':len(cohort),'predicted':predicted,'observed':observed})
            brier.extend((r['probability']-int(r['pnl']>0))**2 for r in cohort)
    enough=len(rows)>=min_groups and sum(r['selected'] for r in rows)>=5 and sum(not r['selected'] for r in rows)>=5
    promoted=bool(enough and lower is not None and lower>0 and drawdown(selected)<=drawdown(baseline))
    return {'status':'promotable' if promoted else 'rejected' if enough else 'collecting',
            'groups':len(rows),'baseline_net':sum(baseline),'policy_net':sum(selected),
            'baseline_expectancy':sum(baseline)/len(rows) if rows else None,
            'policy_expectancy':sum(selected)/len(rows) if rows else None,
            'improvement_lower':lower,'baseline_drawdown':drawdown(baseline),'policy_drawdown':drawdown(selected),
            'brier':sum(brier)/len(brier) if brier else None,'calibration':uncertainty,
            'evidence_ids':[r['id'] for r in rows], 'profitability_proven':False}


def compare_sizing(rows,config):
    """Chronological paired cost model, never an executable counterfactual claim."""
    from capital_policy import evidence_stats,asdict
    history=[];comparison=[];ids=set()
    for row in sorted(rows,key=lambda r:(r['entry_at'],r['id'])):
        if row['id'] in ids:continue
        ids.add(row['id'])
        required=('cost_usd','equity_usd','fixed_cost_usd','pnl')
        if any(type(row.get(k)) not in (int,float) or not math.isfinite(row[k]) for k in required) or row['cost_usd']<=0 or row['equity_usd']<=0 or row['fixed_cost_usd']<0:
            return {'status':'blocked','reason':'Verified cost, equity and fixed-cost observations required'}
        if row.get('eligible') is not True or row.get('synthetic') or row.get('net_of_costs_verified') is not True:
            continue
        prior=[r for r in history if r['exit_at']<row['entry_at'] and r['group_id']!=row['group_id']]
        stats=evidence_stats(prior,config,now=row['entry_at'])
        fraction=min(1.,row['equity_usd']*min(stats['kelly_fraction'],config.max_portfolio_fraction)/row['cost_usd'])
        adjusted=(row['pnl']+row['fixed_cost_usd'])*fraction-row['fixed_cost_usd'] if fraction else 0.
        comparison.append(dict(id=row['id'],group_id=row['group_id'],exit_at=row['exit_at'],pnl=row['pnl'],
            fraction=1.,selected=bool(fraction)))
        comparison[-1]['adjusted_pnl']=adjusted
        comparison[-1]['policy_pnl']=adjusted
        history.append(dict(row,net_return=row['pnl']/row['cost_usd']))
    # Preserve the actual baseline while comparing modeled resized net proceeds.
    report=compare(comparison,config.min_samples,config.bootstrap_samples)
    differences=[r['adjusted_pnl']-r['pnl'] for r in comparison]
    report.update(modeled_sizing_net=sum(r['adjusted_pnl'] for r in comparison),
                  modeled_sizing_improvement=sum(differences),policy=asdict(config),
                  status='modeled_only',live_eligible=False,
                  limitation='Fixed-cost-adjusted linear payoff model; no executable counterfactual fills')
    return report
