"""Bounded multi-process paper replay over one compressed local observation journal."""
import hashlib
import json
import os
from pathlib import Path
import resource
import sqlite3
import time
import uuid
from dataclasses import asdict
from capital_policy import CapitalConfig,CapitalPolicy
from paper_challengers import ChallengerConfig,PaperManagement
from paper_runtime import encode_state,decode_state
from meme_dip import DipRules,PaperDipTrader
from production_strategy import validate

MAX_SNAPSHOT=65536
MAX_STATE=4_000_000


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def identity(value):return hashlib.sha256(encoded(value)).hexdigest()


def decode_snapshot(payload):
    import zstandard
    if payload is None or len(payload)>66560:raise ValueError('Compressed history exceeds frame budget')
    with zstandard.ZstdDecompressor(max_window_size=8192).stream_reader(payload) as stream:
        raw=stream.read(MAX_SNAPSHOT+1)
    if len(raw)>MAX_SNAPSHOT:raise ValueError('Decompressed history exceeds snapshot budget')
    return json.loads(raw)


class Lab:
    def __init__(self,path,create=False):
        self.path=Path(path)
        if not self.path.exists() and not create:raise ValueError('Initialize an isolated challenger journal explicitly')
        if create:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with self.path.open('xb'):pass
            self.path.chmod(0o600)
        self.db=sqlite3.connect(self.path,timeout=30)
        self.db.execute('PRAGMA foreign_keys=ON');self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA cache_size=-2048');self.db.execute('PRAGMA mmap_size=0')
        if create:
            self.db.executescript('''CREATE TABLE history(id INTEGER PRIMARY KEY,hash TEXT UNIQUE,stamp REAL,payload BLOB);
              CREATE TABLE scenarios(id TEXT PRIMARY KEY,spec TEXT,cursor INTEGER,state BLOB,status TEXT,lease TEXT,expires REAL,error TEXT);
              CREATE TABLE seeds(id TEXT PRIMARY KEY,body TEXT);
              CREATE TABLE events(scenario TEXT,history_id INTEGER,body TEXT,PRIMARY KEY(scenario,history_id),FOREIGN KEY(history_id) REFERENCES history(id));
              CREATE TABLE decisions(scenario TEXT,history_id INTEGER,ordinal INTEGER,body TEXT,PRIMARY KEY(scenario,history_id,ordinal));
              CREATE TABLE worker_metrics(id INTEGER PRIMARY KEY,at REAL,body TEXT);''')
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='scenarios'").fetchone():raise ValueError('Not a challenger journal')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS scenario_freezes(id TEXT PRIMARY KEY,at REAL,spec_hash TEXT);
          CREATE TABLE IF NOT EXISTS challenger_nominations(family TEXT PRIMARY KEY,candidate TEXT,at REAL,spec_hash TEXT);
          CREATE TABLE IF NOT EXISTS nomination_history(seq INTEGER PRIMARY KEY,family TEXT,candidate TEXT,at REAL,spec_hash TEXT);''')

    def close(self):self.db.close()

    def ingest_paper(self,path,rules,limit=64):
        """Read-only incremental bridge; duplicate hash recovery is safe after a crash."""
        self.db.executescript('''CREATE TABLE IF NOT EXISTS producers(path TEXT PRIMARY KEY,cursor INTEGER);
          CREATE TABLE IF NOT EXISTS ingest_errors(path TEXT,sequence INTEGER,reason TEXT,PRIMARY KEY(path,sequence));''')
        path=Path(path).resolve()
        if not path.is_file():return {'status':'waiting_for_paper_journal'}
        saved=self.db.execute('SELECT cursor FROM producers WHERE path=?',(str(path),)).fetchone()
        connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            rows=connection.execute('SELECT sequence,CASE WHEN length(snapshot)<=65536 THEN snapshot END FROM paper_events WHERE sequence>? ORDER BY sequence LIMIT ?',
                                    (saved[0] if saved else 0,min(64,max(1,limit)))).fetchall()
        finally:connection.close()
        imported=0
        for sequence,body in rows:
            try:
                snapshot=json.loads(body) if body else None
                if snapshot and snapshot.get('mint'):
                    self.append(snapshot,rules);imported+=1
            except (ValueError,KeyError,TypeError) as error:
                with self.db:self.db.execute('INSERT OR IGNORE INTO ingest_errors VALUES(?,?,?)',(str(path),sequence,type(error).__name__))
            with self.db:self.db.execute('INSERT OR REPLACE INTO producers VALUES(?,?)',(str(path),sequence))
        return {'status':'bounded_import','imported':imported,'read':len(rows)}

    def add_seeds(self,name,rows):
        if len(rows)>1024 or len(encoded(rows))>1_000_000:raise ValueError('Seed evidence exceeds bounded input')
        body=encoded(rows).decode()
        old=self.db.execute('SELECT body FROM seeds WHERE id=?',(name,)).fetchone()
        if old and old[0]!=body:raise ValueError('Seed set is immutable')
        with self.db:self.db.execute('INSERT OR IGNORE INTO seeds VALUES(?,?)',(name,body))

    def append(self,snapshot,rules):
        from meme_learning import decision_context
        from data_policy import require_learning_rights
        raw=encoded(snapshot)
        if len(raw)>MAX_SNAPSHOT:raise ValueError('Snapshot exceeds bounded replay input')
        context=decision_context(snapshot,rules)
        if context.get('data_rights',{}).get('learning_allowed') is not True:raise ValueError('Explicit source learning permission required for challenger replay')
        require_learning_rights(context)
        import zstandard
        compressed=zstandard.ZstdCompressor(level=15,threads=0).compress(raw)
        with self.db:
            previous=self.db.execute('SELECT stamp FROM history ORDER BY id DESC LIMIT 1').fetchone()
            if previous and snapshot['observed_at']<previous[0]:raise ValueError('History must be appended chronologically')
            self.db.execute('INSERT OR IGNORE INTO history(hash,stamp,payload) VALUES(?,?,?)',
                            (identity(snapshot),snapshot['observed_at'],compressed))

    def register(self,scenario,spec):
        if len(encoded(spec))>16384:raise ValueError('Scenario configuration exceeds budget')
        validate(DipRules(**spec['rules']));ChallengerConfig(**spec.get('challengers',{}));CapitalConfig(**spec.get('capital',{}))
        existing=self.db.execute('SELECT spec FROM scenarios WHERE id=?',(scenario,)).fetchone()
        if existing:
            if existing[0]!=encoded(spec).decode():raise ValueError('Scenario configuration is immutable; use another ID')
            with self.db:self.db.execute('INSERT OR IGNORE INTO scenario_freezes VALUES(?,?,?)',(scenario,time.time(),identity(spec)))
            return
        if self.db.execute('SELECT count(*) FROM scenarios').fetchone()[0]>=10000:raise ValueError('Maximum 10000 independent scenarios')
        initial=spec['initial']
        trader=PaperDipTrader(initial['balance'],DipRules(**spec['rules']),initial.get('native_sol'),initial.get('sol_price_usd'))
        trader.challenger_config=spec.get('challengers') or None
        state=dict(trader=encode_state(trader),capital=None)
        with self.db:self.db.execute('INSERT INTO scenarios VALUES(?,?,0,?,\'active\',NULL,0,NULL)',
                                    (scenario,encoded(spec).decode(),encoded(state)))
        with self.db:self.db.execute('INSERT INTO scenario_freezes VALUES(?,?,?)',(scenario,time.time(),identity(spec)))

    def nominate(self,candidate):
        row=self.db.execute('SELECT spec FROM scenarios WHERE id=?',(candidate,)).fetchone()
        if not row or candidate=='baseline':raise ValueError('Nominate a registered management candidate')
        spec=json.loads(row[0]);family=spec.get('family',candidate)
        old=self.db.execute('SELECT candidate FROM challenger_nominations WHERE family=?',(family,)).fetchone()
        if old and old[0]==candidate:return {'family':family,'candidate':candidate,'status':'already_nominated'}
        at=time.time();frozen=identity(spec)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO challenger_nominations VALUES(?,?,?,?)',(family,candidate,at,frozen))
            self.db.execute('INSERT INTO nomination_history(family,candidate,at,spec_hash) VALUES(?,?,?,?)',(family,candidate,at,frozen))
        return {'family':family,'candidate':candidate,'confirmation_begins_at':at,'live_eligible':False}

    def claim(self):
        token=uuid.uuid4().hex;now=time.time()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row=self.db.execute("SELECT id,CASE WHEN length(spec)<=16384 THEN spec END,cursor,CASE WHEN length(state)<=4000000 THEN state END FROM scenarios WHERE status='active' AND expires<? AND cursor<(SELECT coalesce(max(id),0) FROM history) ORDER BY cursor,id LIMIT 1",(now,)).fetchone()
            if row:self.db.execute('UPDATE scenarios SET lease=?,expires=? WHERE id=?',(token,now+120,row[0]))
        return (row,token) if row else (None,None)

    def batch(self,event_batch=16,rss_limit=256_000_000):
        if not 1<=event_batch<=64:raise ValueError('Replay batch must be 1..64 observations')
        row,token=self.claim()
        if not row:return 0
        self.claimed=(row[0],token)
        scenario,spec_text,cursor,raw_state=row
        spec=json.loads(spec_text);state=json.loads(raw_state)
        working=sqlite3.connect(':memory:')
        policy=CapitalPolicy(working,CapitalConfig(**spec.get('capital',{})))
        if state['capital']:
            with working:
                for table,values in state['capital'].items():
                    working.execute('DELETE FROM '+table)
                    if values:working.executemany('INSERT INTO '+table+' VALUES('+','.join('?' for _ in values[0])+')',values)
        else:
            seed_row=self.db.execute('SELECT body FROM seeds WHERE id=?',(spec.get('seed_set',''),)).fetchone()
            seeds=json.loads(seed_row[0]) if seed_row else []
            if len(seeds)>1024:raise ValueError('Seed batch exceeds budget')
            for seed in seeds:
                if seed.get('exit_at',float('inf'))>=spec['start_at']:raise ValueError('Seed evidence occurs after replay starts')
                with working:policy.record(seed)
        trader=decode_state(state['trader']);trader.capital_policy=policy;trader.max_positions=policy.capacity()
        if spec.get('challengers'):trader.paper_management=PaperManagement(ChallengerConfig(**spec['challengers']))
        import zstandard
        observations=self.db.execute('SELECT id,stamp,CASE WHEN length(payload)<=66560 THEN payload END,hash FROM history WHERE id>? ORDER BY id LIMIT ?',(cursor,event_batch)).fetchall()
        events=[];decisions=[]
        try:
            rss=0
            for history_id,stamp,payload,expected_hash in observations:
                if stamp<spec['start_at']:cursor=history_id;continue
                if payload is None:raise ValueError('Compressed history exceeds frame budget')
                snapshot=decode_snapshot(payload)
                if identity(snapshot)!=expected_hash:raise ValueError('History evidence hash mismatch')
                held=next((dict(p) for p in trader.all_positions() if p['mint']==snapshot['mint']),None)
                event=trader.on_snapshot(snapshot)
                event.update(model_only=True,live_eligible=False,entry_signal='momentum_reclaim')
                state['synthetic_seen']=state.get('synthetic_seen',False) or snapshot.get('synthetic',False)
                event['synthetic']=state['synthetic_seen']
                event['portfolio_pnl_usd']=trader.strategy_realized_pnl_usd+sum(p.get('marked_net_pnl_usd',0) for p in trader.all_positions())
                event['marks_complete']=snapshot.get('coverage',{}).get('complete') is True and all(0<=stamp-p.get('last_quote_at',0)<=30 for p in trader.all_positions())
                event['observed_at']=stamp
                if event.get('action')=='paper_sell' and held:
                    cost=held.get('total_cost_usd',held['cash_spent']+held.get('entry_native_cost_usd',0))
                    pnl=event.get('trade_pnl_usd',event['pnl_usd'])
                    with working:policy.record(dict(id=identity([scenario,history_id]),group_id=held['mint'],
                        exit_at=stamp,pnl=pnl,net_return=pnl/cost,cost_usd=cost,eligible=True,model_only=True,context={}))
                trader.max_positions=policy.capacity()
                events.append((scenario,history_id,encoded(event).decode()))
                for ordinal,(body,) in enumerate(working.execute('SELECT body FROM capital_decisions ORDER BY rowid')):
                    value=json.loads(body);value['id']=identity([scenario,history_id,ordinal])
                    decisions.append((scenario,history_id,ordinal,encoded(value).decode()))
                working.execute('DELETE FROM capital_decisions');working.commit()
                cursor=history_id
                rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1024 if os.uname().sysname=='Linux' else 1)
                if rss>rss_limit:raise MemoryError('Worker RSS limit reached; lease retained for recovery')
            dump={table:working.execute('SELECT * FROM '+table+(' ORDER BY at DESC LIMIT 1024' if table=='capital_outcomes' else '')).fetchall()
                  for table in ('capital_policy','capital_outcomes','capital_revocations')}
            checkpoint=encoded(dict(trader=encode_state(trader),capital=dump,synthetic_seen=state.get('synthetic_seen',False)))
            if len(checkpoint)>MAX_STATE:raise MemoryError('Scenario checkpoint exceeds budget')
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                owner=self.db.execute('SELECT lease FROM scenarios WHERE id=?',(scenario,)).fetchone()
                if owner[0]!=token:return 0
                self.db.executemany('INSERT OR IGNORE INTO events VALUES(?,?,?)',events)
                self.db.executemany('INSERT OR IGNORE INTO decisions VALUES(?,?,?,?)',decisions)
                self.db.execute('UPDATE scenarios SET cursor=?,state=?,lease=NULL,expires=0 WHERE id=?',(cursor,checkpoint,scenario))
                self.db.execute('INSERT INTO worker_metrics(at,body) VALUES(?,?)',(time.time(),encoded(dict(pid=os.getpid(),peak_rss_bytes=rss if observations else 0,observations=len(events))).decode()))
            return len(observations)
        except (ValueError,KeyError,TypeError,zstandard.ZstdError) as error:
            with self.db:self.db.execute("UPDATE scenarios SET status='paused',error=?,lease=NULL,expires=0 WHERE id=? AND lease=?",(type(error).__name__+': '+str(error)[:180],scenario,token))
            return -1
        finally:working.close()

    def evaluate(self,candidate,baseline='baseline'):
        from challenger_registry import Registry
        specs=[];cursors=[]
        for key in (baseline,candidate):
            row=self.db.execute('SELECT spec,cursor FROM scenarios WHERE id=?',(key,)).fetchone()
            if not row:raise ValueError('Unknown comparison scenario')
            specs.append(json.loads(row[0]));cursors.append(row[1])
        if any(specs[0].get(k)!=specs[1].get(k) for k in ('rules','initial','capital','seed_set','start_at')):
            raise ValueError('Comparison changes baseline risk, funds or input cohort')
        horizon=min(cursors)
        def daily(key):
            sql="""WITH days AS (SELECT cast(json_extract(body,'$.observed_at')/86400 AS INTEGER) day,max(history_id) hid
                     FROM events WHERE scenario=? AND history_id<=? GROUP BY day)
                 SELECT day,hid,json_extract(body,'$.portfolio_pnl_usd'),json_extract(body,'$.marks_complete'),json_extract(body,'$.synthetic')
                 FROM days JOIN events ON events.scenario=? AND events.history_id=hid ORDER BY day LIMIT 4096"""
            return {r[0]:r[1:] for r in self.db.execute(sql,(key,horizon,key))}
        base,trial=daily(baseline),daily(candidate)
        rows=[];previous=None
        family=specs[1].get('family',candidate)
        nomination=self.db.execute('SELECT candidate,at,spec_hash FROM challenger_nominations WHERE family=?',(family,)).fetchone()
        freeze=self.db.execute('SELECT at FROM scenario_freezes WHERE id=?',(candidate,)).fetchone()
        confirmed=bool(nomination and nomination[0]==candidate and nomination[2]==identity(specs[1]))
        frozen=max(specs[1].get('training_cutoff',specs[1]['start_at']-1),freeze[0] if freeze else time.time(),nomination[1] if confirmed else time.time())
        for day in sorted(base.keys() & trial.keys()):
            a,b=base[day],trial[day]
            if a[0]!=b[0] or not a[2] or not b[2]:previous=None;continue
            if previous and previous[0]==day-1:
                rows.append(dict(id=identity([baseline,candidate,day,horizon]),family=specs[1].get('family',candidate),candidate=candidate,
                    group_id=str(day),entry_at=day*86400,exit_at=(day+1)*86400-1,
                    prediction_at=frozen,policy_frozen_at=frozen,baseline_matched=True,risk_bypass=False,
                    entry_signal='momentum_reclaim',net_of_costs_verified=True,synthetic=bool(a[3] or b[3]),
                    baseline_pnl=a[1]-previous[1],candidate_pnl=b[1]-previous[2]))
            previous=(day,a[1],b[1])
        result=Registry(self.db).evaluate(family,candidate,rows)
        result.update(confirmation_nominated=confirmed,confirmation_after=frozen,
                      historical_replay_not_promotion_evidence=True)
        return result


def worker(path,jobs,event_batch,rss_limit):
    lab=Lab(path)
    try:
        for _ in range(jobs):
            try:
                if lab.batch(event_batch,rss_limit)==0:break
            except (ValueError,KeyError,TypeError,MemoryError) as error:
                with lab.db:
                    lab.db.execute('INSERT INTO worker_metrics(at,body) VALUES(?,?)',(time.time(),encoded(dict(error=type(error).__name__,reason=str(error)[:180])).decode()))
                    if not isinstance(error,MemoryError) and getattr(lab,'claimed',None):
                        scenario,token=lab.claimed
                        lab.db.execute("UPDATE scenarios SET status='paused',error=?,lease=NULL,expires=0 WHERE id=? AND lease=?",(str(error)[:180],scenario,token))
                if isinstance(error,MemoryError):raise
    finally:lab.close()
