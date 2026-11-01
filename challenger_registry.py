"""Append-only paper challenger qualification, promotion and rollback journal."""
import json
import time
import hashlib
import math
import sqlite3
from pathlib import Path
from expectancy_validation import compare


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


class Registry:
    def __init__(self,db):
        self.db=db
        db.executescript('''CREATE TABLE IF NOT EXISTS challenger_evaluations(id TEXT PRIMARY KEY,family TEXT,candidate TEXT,body TEXT);
          CREATE TABLE IF NOT EXISTS challenger_transitions(seq INTEGER PRIMARY KEY,family TEXT,at REAL,body TEXT);
          CREATE TABLE IF NOT EXISTS challenger_champions(family TEXT PRIMARY KEY,candidate TEXT,evaluation TEXT);''')
        db.execute('CREATE TABLE IF NOT EXISTS challenger_evaluation_events(seq INTEGER PRIMARY KEY,evaluation TEXT)')

    def evaluate(self,family,candidate,rows,*,min_groups=30,minimum_days=14,min_net_improvement=0.,max_drawdown_ratio=1.):
        if not family or not candidate or len(rows)>4096 or min_groups<30 or minimum_days<14 or min_net_improvement<0 or not 0<max_drawdown_ratio<=1:
            raise ValueError('Bounded named challenger evidence and conservative validation parameters required')
        accepted=[];seen=set()
        for row in rows:
            if not row.get('id') or not row.get('group_id') or any(type(row.get(k)) not in (int,float) or not math.isfinite(row[k]) for k in
                ('entry_at','exit_at','prediction_at','policy_frozen_at','baseline_pnl','candidate_pnl')):continue
            if row.get('id') in seen:continue
            seen.add(row.get('id'))
            if (row.get('candidate')!=candidate or row.get('family')!=family or row.get('synthetic')
                or row.get('net_of_costs_verified') is not True or row.get('entry_signal')!='momentum_reclaim'
                or row.get('prediction_at',float('inf'))>row.get('entry_at',0)
                or row['prediction_at']<row['policy_frozen_at'] or row['exit_at']<=row['entry_at']
                or row.get('policy_frozen_at',float('inf'))>=row.get('entry_at',0)
                or row.get('baseline_matched') is not True or row.get('risk_bypass') is not False):continue
            accepted.append(dict(row,pnl=row['baseline_pnl'],selected=True,policy_pnl=row['candidate_pnl']))
        result=compare(accepted,min_groups)
        # Paired management policies can all trade; selection-count requirements do not apply.
        days=len({int(row['exit_at']//86400) for row in accepted})
        enough=len(accepted)>=min_groups and days>=minimum_days
        lower=result['improvement_lower']
        net=result['policy_net']-result['baseline_net']
        passed=enough and lower is not None and lower>min_net_improvement and net>0 and result['policy_drawdown']<=result['baseline_drawdown']*max_drawdown_ratio
        result.update(status='qualified' if passed else 'rejected' if enough else 'collecting',days=days,
                      family=family,candidate=candidate,scope='paper_management',live_eligible=False)
        for name,key in (('baseline','baseline_pnl'),('policy','candidate_pnl')):
            values=[r[key] for r in accepted]
            downside=math.sqrt(sum(min(0,v)**2 for v in values)/len(values)) if values else None
            result[name+'_downside_rms']=downside
            result[name+'_net_over_downside']=(sum(values)/len(values)/downside) if downside else None
        key=digest(result)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO challenger_evaluations VALUES(?,?,?,?)',(key,family,candidate,json.dumps(result,allow_nan=False)))
            self.db.execute('INSERT INTO challenger_evaluation_events(evaluation) VALUES(?)',(key,))
        return dict(result,evaluation_id=key)

    def transition(self,family,candidate,evaluation,action):
        if action not in ('promote','rollback'):raise ValueError('Unknown challenger transition')
        row=self.db.execute('SELECT family,candidate,body FROM challenger_evaluations WHERE id=?',(evaluation,)).fetchone()
        if not row or row[:2]!=(family,candidate):raise ValueError('Evaluation/candidate binding mismatch')
        report=json.loads(row[2])
        if report['status']!='qualified':raise ValueError('Unqualified challenger cannot become a champion or rollback target')
        if action=='rollback' and not self.db.execute("SELECT 1 FROM challenger_transitions WHERE family=? AND json_extract(body,'$.candidate')=? AND json_extract(body,'$.action')='promote' LIMIT 1",(family,candidate)).fetchone():
            raise ValueError('Rollback target was never a qualified champion')
        with self.db:
            old=self.db.execute('SELECT candidate,evaluation FROM challenger_champions WHERE family=?',(family,)).fetchone()
            self.db.execute('INSERT INTO challenger_transitions(family,at,body) VALUES(?,?,?)',
                (family,time.time(),json.dumps(dict(action=action,previous=old,candidate=candidate,evaluation=evaluation))))
            self.db.execute('INSERT OR REPLACE INTO challenger_champions VALUES(?,?,?)',(family,candidate,evaluation))
        return {'scope':'paper_management','family':family,'candidate':candidate,'action':action,'live_eligible':False}


def champion_settings(path,families,rules):
    """Read qualified paper policy only; caller preserves existing-position ownership."""
    from dataclasses import asdict
    from meme_dip import DipRules
    from paper_challengers import ChallengerConfig
    connection=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    settings={}
    try:
        for family in families:
            row=connection.execute('SELECT candidate,evaluation FROM challenger_champions WHERE family=?',(family,)).fetchone()
            if not row:raise ValueError('No qualified paper champion for '+family)
            candidate,evaluation=row
            report=connection.execute('SELECT body FROM challenger_evaluations WHERE id=?',(evaluation,)).fetchone()
            if not report or json.loads(report[0]).get('status')!='qualified':raise ValueError('Paper champion qualification unavailable')
            latest=connection.execute('SELECT e.body FROM challenger_evaluation_events v JOIN challenger_evaluations e ON e.id=v.evaluation WHERE e.family=? AND e.candidate=? ORDER BY v.seq DESC LIMIT 1',(family,candidate)).fetchone()
            if not latest or json.loads(latest[0]).get('status')!='qualified':raise ValueError('Paper champion degraded or requires further evidence')
            spec=connection.execute('SELECT spec FROM scenarios WHERE id=?',(candidate,)).fetchone()
            if not spec:raise ValueError('Champion configuration missing')
            spec=json.loads(spec[0])
            if asdict(DipRules(**spec['rules']))!=asdict(rules):raise ValueError('Champion baseline rules differ from active paper rules')
            for key,value in spec['challengers'].items():
                if key in settings and settings[key]!=value:raise ValueError('Independent champions have conflicting settings')
                settings[key]=value
        ChallengerConfig(**settings)
        return settings
    finally:connection.close()
