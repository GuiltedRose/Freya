"""Cost-adjusted evidence, independent performance tiers and hard Kelly ceilings."""
from dataclasses import dataclass, asdict
import json
import math
import random
import time
import uuid
from statistics import NormalDist


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


@dataclass(frozen=True)
class CapitalConfig:
    version: int = 1
    min_samples: int = 30
    min_wins: int = 5
    min_losses: int = 5
    max_age_seconds: int = 5184000
    confidence_z: float = 1.96
    bootstrap_samples: int = 200
    fractional_kelly: float = .25
    max_kelly_fraction: float = .25
    max_portfolio_fraction: float = .25
    allocation_steps: tuple = (.25,.5,.75,1.)
    allocation_min: int = 0
    allocation_max: int = 3
    allocation_promote_wins: int = 5
    allocation_demote_losses: int = 2
    concurrency_steps: tuple = (1,2,4,8,12,20)
    concurrency_min: int = 0
    concurrency_max: int = 5
    concurrency_promote_wins: int = 10
    concurrency_demote_losses: int = 2

    def __post_init__(self):
        if type(self.version) is not int or self.version<1:raise ValueError('Positive policy version required')
        for name in ('allocation','concurrency'):
            steps=getattr(self,name+'_steps')
            low,high=getattr(self,name+'_min'),getattr(self,name+'_max')
            if not steps or list(steps)!=sorted(set(steps)) or not 0<=low<=high<len(steps):
                raise ValueError('Invalid independent tier bounds')
            for suffix in ('promote_wins','demote_losses'):
                value=getattr(self,name+'_'+suffix)
                if type(value) is not int or value<1:raise ValueError('Invalid tier threshold')
        if any(type(n) is not int or not 1<=n<=20 for n in self.concurrency_steps):
            raise ValueError('Concurrency capacity must be 1..20')
        if not all(0<x<=1 for x in self.allocation_steps) or self.allocation_steps[-1]!=1:
            raise ValueError('Allocation tiers must end at 100% of the calculated ceiling')
        if not all(0<x<=1 for x in (self.fractional_kelly,self.max_kelly_fraction,self.max_portfolio_fraction)):
            raise ValueError('Invalid capital fractions')
        if min(self.min_samples,self.min_wins,self.min_losses,self.max_age_seconds)<1 or not 50<=self.bootstrap_samples<=1000 or not 1<=self.confidence_z<=4:
            raise ValueError('Invalid evidence requirements')


def tier_step(state,pnl,steps,minimum,maximum,wins,losses):
    state=dict(state or dict(tier=minimum,wins=0,losses=0))
    if not minimum<=state['tier']<=maximum:raise ValueError('Saved tier outside configured bounds')
    state['wins']=state['wins']+1 if pnl>0 else 0
    state['losses']=state['losses']+1 if pnl<0 else 0
    if state['wins']>=wins:
        state['tier']=min(maximum,state['tier']+1);state['wins']=0
    if state['losses']>=losses:
        state['tier']=max(minimum,state['tier']-1);state['losses']=0
    return state


def evidence_stats(rows,config=None,now=None):
    cfg=config or CapitalConfig();now=time.time() if now is None else now
    selected={};identities=set()
    for row in sorted(rows,key=lambda r:(r.get('exit_at',0),str(r.get('id','')))):
        identity=row.get('id');group=row.get('group_id')
        stamp=row.get('exit_at');value=row.get('net_return')
        if (not identity or not group or identity in identities or row.get('eligible') is not True
            or type(stamp) not in (int,float) or not 0<=now-stamp<=cfg.max_age_seconds
            or type(value) not in (int,float) or not math.isfinite(value)):
            continue
        identities.add(identity);selected[group]=row
    rows=sorted(selected.values(),key=lambda r:(r['exit_at'],r['id']));values=[r['net_return'] for r in rows]
    wins=[v for v in values if v>0];losses=[-v for v in values if v<0]
    result={'samples':len(values),'wins':len(wins),'losses':len(losses),'evidence_ids':[r['id'] for r in rows],
            'supported':False,'kelly_fraction':0.,'expectancy':sum(values)/len(values) if values else None}
    if len(values)<cfg.min_samples or len(wins)<cfg.min_wins or len(losses)<cfg.min_losses:
        return dict(result,reason='Insufficient independent cost-adjusted outcomes')
    p=len(wins)/len(values);n=len(values);z=cfg.confidence_z
    p_low=(p+z*z/(2*n)-z*math.sqrt((p*(1-p)+z*z/(4*n))/n))/(1+z*z/n)
    rng=random.Random(42);means=[];win_means=[];loss_means=[]
    for _ in range(cfg.bootstrap_samples):
        means.append(sum(rng.choices(values,k=n))/n)
        win_means.append(sum(rng.choices(wins,k=len(wins)))/len(wins))
        loss_means.append(sum(rng.choices(losses,k=len(losses)))/len(losses))
    tail=NormalDist().cdf(-cfg.confidence_z)
    def lower(items):return sorted(items)[int(tail*len(items))]
    def upper(items):return sorted(items)[min(len(items)-1,int((1-tail)*len(items)))]
    w,l=lower(win_means),upper(loss_means)
    expectancy_low=lower(means)
    raw=max(0.,p_low/l-(1-p_low)/w) if w>0 and l>0 and expectancy_low>0 else 0.
    ceiling=min(cfg.max_kelly_fraction,raw*cfg.fractional_kelly,.95/max(losses))
    peak=equity=drawdown=0.
    for value in values:
        equity+=value;peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
    result.update(supported=ceiling>0,kelly_fraction=ceiling,win_probability=p,win_probability_lower=p_low,
        mean_win=sum(wins)/len(wins),mean_loss=sum(losses)/len(losses),payoff_ratio=sum(wins)/len(wins)/(sum(losses)/len(losses)),
        conservative_win=w,conservative_loss=l,expectancy_lower=expectancy_low,
        downside_rms=math.sqrt(sum(min(0,v)**2 for v in values)/n),max_drawdown=drawdown,
        reason='Positive conservative edge' if ceiling>0 else 'Negative or uncertain edge')
    return result


class CapitalPolicy:
    def __init__(self,db,config,*,paper=False,percentage_warmup=False):
        self.paper=paper is True
        self.percentage_warmup=self.paper or percentage_warmup is True
        self.db=db;self.config=config;self.policy_id=config.version
        self.db.executescript('''CREATE TABLE IF NOT EXISTS capital_policy(id INTEGER PRIMARY KEY,config TEXT,state TEXT);
            CREATE TABLE IF NOT EXISTS capital_outcomes(id TEXT PRIMARY KEY,at REAL,body TEXT);
            CREATE TABLE IF NOT EXISTS capital_revocations(id TEXT PRIMARY KEY,at REAL,reason TEXT);
            CREATE TABLE IF NOT EXISTS capital_decisions(id TEXT PRIMARY KEY,at REAL,body TEXT);''')
        latest=db.execute('SELECT max(id) FROM capital_policy').fetchone()[0]
        if latest is not None and config.version<latest:raise ValueError('Policy rollback requires a new reviewed version; existing positions remain manageable')
        row=db.execute('SELECT config FROM capital_policy WHERE id=?',(self.policy_id,)).fetchone()
        encoded=canonical(asdict(config))
        if row and row[0]!=encoded:raise ValueError('Capital policy changed; explicit reviewed policy transition required')
        if not row:
            state={name:dict(tier=getattr(config,name+'_min'),wins=0,losses=0) for name in ('allocation','concurrency')}
            with db:db.execute('INSERT INTO capital_policy VALUES(?,?,?)',(self.policy_id,encoded,canonical(state)))

    def state(self):return json.loads(self.db.execute('SELECT state FROM capital_policy WHERE id=?',(self.policy_id,)).fetchone()[0])

    def record(self,row):
        from data_policy import require_learning_rights,SourceAccessBlocked
        if row.get('eligible') is not True or row.get('synthetic'):return
        try:require_learning_rights(row.get('context',{}))
        except SourceAccessBlocked:return
        if not all(type(row.get(k)) in (int,float) and math.isfinite(row[k]) for k in ('pnl','net_return','exit_at')):
            raise ValueError('Invalid realized capital outcome')
        old=self.db.execute('SELECT body FROM capital_outcomes WHERE id=?',(row['id'],)).fetchone()
        if old:
            if old[0]!=canonical(row):raise ValueError('Conflicting capital outcome')
            return
        latest=self.db.execute('SELECT max(at) FROM capital_outcomes').fetchone()[0]
        backdated=latest is not None and row['exit_at']<latest
        if backdated and not row.get('order_failure'):raise ValueError('Out-of-order tier outcome; explicit reconciliation required')
        state=self.state();cfg=self.config
        for name in ('allocation','concurrency'):
            state[name]=(dict(tier=getattr(cfg,name+'_min'),wins=0,losses=0) if backdated else
                tier_step(state[name],row['pnl'],getattr(cfg,name+'_steps'),getattr(cfg,name+'_min'),getattr(cfg,name+'_max'),
                getattr(cfg,name+'_promote_wins'),getattr(cfg,name+'_demote_losses')))
        self.db.execute('INSERT INTO capital_outcomes VALUES(?,?,?)',(row['id'],row['exit_at'],canonical(row)))
        self.db.execute('UPDATE capital_policy SET state=? WHERE id=?',(canonical(state),self.policy_id))

    def capacity(self):return self.config.concurrency_steps[self.state()['concurrency']['tier']]

    def revoke(self,identity,reason):
        if self.db.execute('SELECT 1 FROM capital_revocations WHERE id=?',(identity,)).fetchone():return
        self.db.execute('INSERT INTO capital_revocations VALUES(?,?,?)',(identity,time.time(),reason))
        state={name:dict(tier=getattr(self.config,name+'_min'),wins=0,losses=0) for name in ('allocation','concurrency')}
        self.db.execute('UPDATE capital_policy SET state=? WHERE id=?',(canonical(state),self.policy_id))

    def size(self,equity,existing_limit,exposure,positions,*,ml_fraction=1.,liquidity_limit=None,overhead_usd=0.,existing_position_usd=0.,now=None):
        if not all(math.isfinite(x) and x>=0 for x in (equity,existing_limit,exposure,overhead_usd,existing_position_usd)) or not 0<=ml_fraction<=1:
            raise ValueError('Invalid sizing input')
        if type(positions) is not int or positions<0 or liquidity_limit is not None and (not math.isfinite(liquidity_limit) or liquidity_limit<0):
            raise ValueError('Invalid portfolio capacity or liquidity')
        rows=[json.loads(r[0]) for r in self.db.execute('SELECT body FROM capital_outcomes WHERE id NOT IN (SELECT id FROM capital_revocations) ORDER BY at DESC LIMIT 1024')]
        stamp=time.time() if now is None else now
        failure_rows=[r for r in rows if r.get('order_failure') and 0<=stamp-r['exit_at']<=self.config.max_age_seconds]
        failures=sum(max(0,-r['pnl']) for r in failure_rows)
        trade_rows=[r for r in rows if not r.get('order_failure')]
        latest={}
        for r in sorted(trade_rows,key=lambda x:x['exit_at']):
            if 0<=stamp-r['exit_at']<=self.config.max_age_seconds:latest[r['group_id']]=r
        basis=sum(r.get('cost_usd',0) for r in latest.values())
        if failures and basis>0:
            trade_rows=[dict(r,net_return=r['net_return']-failures/basis) for r in trade_rows]
        stats=evidence_stats(trade_rows,self.config,now);state=self.state()
        stats['failed_order_cost_usd']=failures;stats['failed_order_evidence_ids']=[r['id'] for r in failure_rows]
        if failures and basis<=0:stats.update(kelly_fraction=0.,supported=False,reason='Failed costs lack verified capital basis')
        ceiling=equity*stats['kelly_fraction']
        unlocked=ceiling*self.config.allocation_steps[state['allocation']['tier']]
        # Start learning with a reduced existing percentage budget. This is
        # an allocation policy, not evidence of a measured Kelly edge.
        warming_up=(self.percentage_warmup and not stats['supported'] and not failures
                  and stats.get('reason')=='Insufficient independent cost-adjusted outcomes'
                  and (not rows or stats['samples']>0)
                  and (stats['expectancy'] is None or stats['expectancy']>0)
                  and not self.db.execute('SELECT 1 FROM capital_revocations LIMIT 1').fetchone())
        if warming_up:unlocked=existing_limit*self.config.allocation_steps[self.config.allocation_min]
        maximum=max(0,min(unlocked-overhead_usd-existing_position_usd,existing_limit,equity*self.config.max_portfolio_fraction-exposure-overhead_usd,
                          liquidity_limit if liquidity_limit is not None else 0))
        if positions>=self.capacity():maximum=0.
        result={'allocation_usd':maximum*ml_fraction,'hard_maximum_usd':maximum,'kelly_ceiling_usd':ceiling,
                'sizing_mode':('paper_percentage_warmup' if self.paper else 'live_percentage_warmup') if warming_up else 'kelly',
                'ml_fraction':ml_fraction,'equity_usd':equity,'exposure_usd':exposure,'capacity':self.capacity(),
                'reserved_overhead_usd':overhead_usd,
                'tiers':state,'evidence':stats,'policy':asdict(self.config),'id':str(uuid.uuid4())}
        with self.db:self.db.execute('INSERT INTO capital_decisions VALUES(?,?,?)',(result['id'],time.time(),canonical(result)))
        return result


def attach_live(engine,path):
    policy=CapitalPolicy(engine.store.db,load_config(path),percentage_warmup=getattr(engine,'neural_mode','learning')!='qualified')
    engine.capital_policy=engine.store.capital_policy=policy
    state=engine.store.state
    if 'positions' not in state:
        state['positions']={p['mint']:p for p in engine.store.all_positions()}
        state['position_owner']=state['position']['mint'] if state['position'] else None
        engine.store.save('portfolio_accounting_enabled',{'capacity':policy.capacity()})
    return policy


def seed(policy,outcomes):
    """Import only existing realized labels with known cost basis; never infer labels."""
    from data_policy import require_learning_rights,SourceAccessBlocked
    pending_failures=[]
    if policy.db.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone():
        for event_id,at,body in policy.db.execute("SELECT id,at,body FROM events WHERE kind='transaction_failed' ORDER BY id").fetchall():
            event=json.loads(body);fee=event.get('network_cost_usd')
            if type(fee) not in (int,float) or not math.isfinite(fee) or fee<0:continue
            failure=event.get('capital_outcome') or dict(id='failed-event:'+str(event_id),group_id='failed-order',
                exit_at=at,pnl=-fee,net_return=0.,eligible=True,order_failure=True,context={})
            pending_failures.append(failure)
    valid={identity for identity,body in policy.db.execute('SELECT id,body FROM capital_outcomes') if json.loads(body).get('order_failure')}
    for row in sorted(outcomes,key=lambda r:(r['exit_at'],r['exit_signature'])):
        cost=row.get('cost_usd')
        if type(cost) not in (int,float) or cost<=0 or row.get('training_eligible',True) is not True:continue
        item=dict(id=row['exit_signature'],group_id=row['mint'],exit_at=row['exit_at'],pnl=row['net_pnl_usd'],
            net_return=row['net_pnl_usd']/cost,cost_usd=cost,eligible=True,context=row.get('entry_context') or {})
        try:require_learning_rights(item['context'])
        except SourceAccessBlocked:continue
        valid.add(item['id'])
        if policy.db.execute('SELECT 1 FROM capital_outcomes WHERE id=?',(item['id'],)).fetchone():continue
        with policy.db:policy.record(item)
    for failure in pending_failures:
        valid.add(failure['id'])
        if not policy.db.execute('SELECT 1 FROM capital_outcomes WHERE id=?',(failure['id'],)).fetchone():
            with policy.db:policy.record(failure)
    for identity, in policy.db.execute('SELECT id FROM capital_outcomes').fetchall():
        if identity not in valid:
            with policy.db:policy.revoke(identity,'Outcome excluded by authoritative ledger or source eligibility')


def load_config(path):
    with open(path) as stream:return CapitalConfig(**json.load(stream))
