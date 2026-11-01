"""Evidence-bound context, separate from scam labels and entry signals."""
import hashlib
import json
import math

RISK_FEATURES=('top_owner_fraction','top_ten_fraction','developer_linked_fraction',
    'coordinated_fraction','suspected_wash_fraction','circular_flow_fraction',
    'same_slot_fraction','liquidity_removed_fraction','mint_authority_enabled',
    'freeze_authority_enabled','transfer_restriction','funding_cluster_fraction')
RISK_KINDS={'rug_pull','pump_and_dump','wash_trading','concentrated_supply',
            'developer_linked','coordinated_trading','independently_reviewed_safe'}


def finite(value):return type(value) in (int,float) and math.isfinite(value)


def closed_bars(snapshot):
    history=snapshot.get('candle_history') or {}
    if not isinstance(history,dict) or not finite(snapshot.get('observed_at')):return []
    if history.get('mint')!=snapshot.get('mint') or history.get('pool')!=snapshot.get('pool'):return []
    raw=history.get('candles',[])
    if not isinstance(raw,list):return []
    now=snapshot['observed_at'];bars=raw[-12:]
    previous=None
    for bar in bars:
        if not isinstance(bar,dict):return []
        values=[bar.get(k) for k in ('timestamp','open','high','low','close','volume_usd')]
        if (not all(finite(v) for v in values) or bar.get('final') is False
            or bar['timestamp']+60>now or previous is not None and bar['timestamp']-previous!=60
            or not 0<bar['low']<=min(bar['open'],bar['close'])<=max(bar['open'],bar['close'])<=bar['high']
            or bar['volume_usd']<0):return []
        previous=bar['timestamp']
    return bars


def lifecycle(snapshot,new_age=1800,established_age=86400,unstable_range=.3,consolidation_range=.03):
    now=snapshot.get('observed_at');bars=closed_bars(snapshot)
    created=snapshot.get('token_created_at')
    age=now-created if finite(now) and finite(created) and 0<=created<=now else None
    regime='unknown'
    if len(bars)>=3:
        spread=(max(b['high'] for b in bars)-min(b['low'] for b in bars))/bars[-1]['close']
        gain=bars[-1]['close']/bars[0]['open']-1
        if spread>unstable_range:regime='unstable'
        elif age is not None and age<new_age:regime='new_launch'
        elif spread<consolidation_range:regime='consolidation'
        elif gain>.05 and bars[-1]['volume_usd']>sum(b['volume_usd'] for b in bars[:-1])/max(1,len(bars)-1):regime='renewed_pump'
        elif age is not None and age>=established_age:regime='established'
    return {'regime':regime,'age_seconds':age,'classification_only':True,
            'risk_label':None,'independent_human_verified':False}


def risk_features(snapshot,max_age=60,min_coverage=.8):
    evidence=snapshot.get('risk_evidence') or {}
    if not isinstance(evidence,dict) or not finite(snapshot.get('observed_at')):return [None]*len(RISK_FEATURES)
    valid=(evidence.get('mint')==snapshot.get('mint') and evidence.get('pool')==snapshot.get('pool')
        and evidence.get('verified') is True and bool(evidence.get('evidence'))
        and finite(evidence.get('observed_at')) and 0<=snapshot['observed_at']-evidence['observed_at']<=max_age
        and finite(evidence.get('coverage')) and min_coverage<=evidence['coverage']<=1)
    values=evidence.get('features',{}) if valid else {}
    if not isinstance(values,dict):values={}
    return [values.get(key) if finite(values.get(key)) and 0<=values[key]<=1 else None for key in RISK_FEATURES]


def risk_schema():return hashlib.sha256(json.dumps(RISK_FEATURES).encode()).hexdigest()


def verified_risk_label(row):
    proof=row.get('risk_label_evidence') or {}
    if not isinstance(proof,dict):return False
    return (row.get('risk_label_verified') is True and row.get('risk_label') in (0,1)
        and proof.get('reviewed') is True and bool(proof.get('evidence'))
        and proof.get('kind') in RISK_KINDS
        and proof.get('basis') in ('onchain_transaction_analysis','independent_security_review','verified_incident_analysis')
        and finite(row.get('risk_label_at')) and row['risk_label_at']>=row['feature_at']
        and (row['risk_label']==0)==(proof.get('kind')=='independently_reviewed_safe'))


def risk_tensor(row):
    values=row.get('risk_features')
    if row.get('risk_schema')!=risk_schema() or not isinstance(values,list) or len(values)!=len(RISK_FEATURES):
        raise ValueError('Verified risk feature schema required')
    if sum(v is not None for v in values)<len(values)*.8:raise ValueError('Risk feature coverage insufficient')
    result=[]
    for value in values:
        if value is not None and (not finite(value) or not 0<=value<=1):raise ValueError('Invalid risk feature')
        result.extend((0. if value is None else value,1. if value is None else 0.))
    return result
