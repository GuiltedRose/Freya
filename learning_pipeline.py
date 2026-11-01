"""Reviewed ledger learning and active model vetoes; no order authority."""
import json
import time
from pathlib import Path
from unified_observations import ObservationStore, import_bot_ledger, import_position_quotes
from unified_observations import add_market_context, digest, number
from unified_learning import build_candidate, forecast, load_candidate, MAX_STEPS


def load_config(path):
    config = json.loads(Path(path).read_text())
    if not isinstance(config.get('provenance'),dict):
        raise ValueError('Reviewed learning provenance required')
    rights = config.get('rights',{})
    if rights.get('storage_verified') is not True or not rights.get('evidence'):
        raise ValueError('Explicit learning storage permission required')
    if len(config.get('ledgers',[])) > 4 or len(config.get('candidates',{})) > 3:
        raise ValueError('Learning configuration exceeds bounds')
    return config


def synchronize(store, config):
    from meme_learning import paper_outcomes, live_outcomes
    report = {'ledgers':[], 'models':{}, 'order_authorization':False}
    paths = []
    for ledger in config.get('ledgers',[]):
        path = Path(ledger['path']).resolve()
        source = ledger['source']
        if source not in ('bot_paper','bot_live'):
            raise ValueError('Wallet imports require reviewed receipt-backed JSONL')
        if not path.is_file():
            report['ledgers'].append({'source':source,'status':'ledger_not_created'})
            continue
        outcomes = (paper_outcomes if source=='bot_paper' else live_outcomes)(path)[0]
        source_count = len(outcomes)
        if source_count > 1024:
            raise ValueError('Ledger exceeds 1024 outcomes; use paged reviewed JSONL imports')
        if source == 'bot_paper':
            import sqlite3
            connection = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
            try:
                archived = {r[0] for r in connection.execute('SELECT id FROM paper_runs WHERE archived IS NOT NULL AND archived!=0')}
            finally:
                connection.close()
            outcomes = [r for r in outcomes if r.get('run_id') not in archived]
        from data_policy import require_learning_rights, SourceAccessBlocked
        active = set()
        for outcome in outcomes:
            if (not outcome.get('training_eligible',True) or not number(outcome.get('net_pnl_usd'))
                or outcome.get('entry_data_kind')=='synthetic'):
                continue
            try:
                require_learning_rights(outcome.get('entry_context',{}))
            except SourceAccessBlocked:
                continue
            active.add(outcome['exit_signature'])
        with store.connect(readonly=True) as db:
            previous = db.execute("SELECT body FROM observations WHERE source=? AND json_extract(body,'$.provenance.ledger_path')=? LIMIT 4097",
                                  (source,str(path))).fetchall()
        if len(previous)>4096:
            raise ValueError('Ledger reconciliation exceeds bounded worker; explicit offline review required')
        for stored in previous:
            row=json.loads(stored[0])
            if row.get('provenance',{}).get('outcome_identity',row['identity']) not in active:
                store.revoke(source,row['identity'],'Source outcome excluded or archived')
        proof = dict(config['provenance'],ledger_path=str(path))
        imported = 0
        for offset in range(0,source_count,128):
            imported += import_bot_ledger(store,path,source,config['rights'],proof,offset)['imported']
        paths.append(path)
        report['ledgers'].append({'source':source,'imported':imported})
    if paths:
        report['quotes']=import_position_quotes(store,paths,config['rights'],config['provenance'])
    for family in ('svm','lstm','rag'):
        candidate = build_candidate(store,family)
        report['models'][family]={k:candidate[k] for k in ('id','status','reason') if k in candidate}
    return report


class DecisionModels:
    def __init__(self, database, config):
        self.store = ObservationStore(database,create=False)
        self.config = load_config(config)

    def entry(self, snapshot, rules, *, live=False):
        import uuid
        from neural_autonomy import THRESHOLD, MAX_AGE
        from unified_learning import evaluate_candidate
        # Streams supply LSTM history; each decision freezes fresh scores separately.
        stream_id='stream:'+snapshot['mint']+':'+snapshot['pool']
        context_id=str(uuid.uuid4())
        from meme_learning import decision_context
        from data_policy import require_learning_rights
        context=decision_context(snapshot,rules)
        require_learning_rights(context)
        stamp=snapshot['observed_at']
        add_market_context(self.store,context,identity=digest([stream_id,stamp]),
            position_id=stream_id,group_id=stream_id,mint=snapshot['mint'],
            feature_at=stamp,known_at=stamp,rights=self.config['rights'],provenance=self.config['provenance'])
        with self.store.connect(readonly=True) as db:
            saved=db.execute('SELECT body FROM observations WHERE position_id=? AND id NOT IN (SELECT id FROM revocations) ORDER BY feature_at DESC LIMIT ?',
                             (stream_id,MAX_STEPS)).fetchall()
        rows=[dict(json.loads(row[0]),position_id=context_id) for row in reversed(saved)]
        candidates=self.config.get('candidates',{})
        predictions={}
        allow=set(candidates)=={'svm','lstm','rag'}
        for family,identifier in candidates.items():
            candidate=load_candidate(self.store,identifier)
            if candidate['family']!=family:
                raise ValueError('Configured model family mismatch')
            if not 0 <= time.time()-candidate['created_at'] <= MAX_AGE:
                raise ValueError('Configured model is expired or dated in the future')
            prediction=forecast(self.store,identifier,rows,context_id)
            if live:
                outcomes,failures=self.confirmed_outcomes()
                qualification=evaluate_candidate(self.store,identifier,outcomes,failures)
                prediction['qualification']=qualification
                allow=allow and qualification.get('live_eligible') is True
            score=prediction.get('score')
            allow=allow and number(score) and score>=THRESHOLD
            predictions[family]=prediction
        return {'allow':bool(allow),'status':'allowed' if allow else 'model_entry_blocked',
                'position_id':context_id,'models':predictions,'required_families':['svm','lstm','rag'],
                'reason':'All configured models must supply eligible scores; live also requires prospective qualification'}

    def confirmed_outcomes(self):
        from meme_learning import live_outcomes
        outcomes=[];failures=[]
        for ledger in self.config.get('ledgers',[]):
            if ledger['source']!='bot_live' or not Path(ledger['path']).is_file():
                continue
            closed,_,failed,_=live_outcomes(ledger['path'])
            failures.extend(failed)
            for row in closed:
                context=row.get('entry_context') or {}
                identity=context.get('model_position_id')
                if not identity:
                    continue
                outcomes.append(dict(row,position_id=identity,rights=self.config['rights'],
                    provenance=self.config['provenance'],net_of_costs_verified=self.config['provenance'].get('net_outcomes_verified') is True))
        if len(outcomes)>1024 or len(failures)>1024:
            raise ValueError('Prospective evaluation exceeds bounded batch')
        return outcomes,failures
