"""Operator-controlled offline scenario worker and independent paper promotions."""
import argparse
import json
import multiprocessing
import itertools
from pathlib import Path
from challenger_lab import Lab,worker
from challenger_registry import Registry


def read_json(path,limit=1_000_000):
    with Path(path).open('rb') as stream:raw=stream.read(limit+1)
    if len(raw)>limit:raise ValueError('Configuration/input byte budget exceeded')
    return json.loads(raw)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--initialize',action='store_true')
    parser.add_argument('--config',type=Path)
    parser.add_argument('--seed-outcomes',type=Path)
    parser.add_argument('--paper-db',type=Path)
    parser.add_argument('--workers',type=int,default=1)
    parser.add_argument('--jobs',type=int,default=128)
    parser.add_argument('--event-batch',type=int,default=16)
    parser.add_argument('--rss-limit-mb',type=int,default=256)
    parser.add_argument('--evaluate')
    parser.add_argument('--nominate',help='Freeze one candidate per family for subsequent forward paper confirmation')
    parser.add_argument('--transition',choices=('promote','rollback'))
    parser.add_argument('--family')
    parser.add_argument('--evaluation-id')
    parser.add_argument('--resume-scenario')
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--list-limit',type=int,default=20)
    args=parser.parse_args()
    if not 1<=args.workers<=8 or not 1<=args.jobs<=10000 or not 1<=args.event_batch<=64 or not 64<=args.rss_limit_mb<=1024:
        parser.error('Worker, batching or RAM limit exceeds allowed bounds')
    lab=Lab(args.db,create=args.initialize)
    try:
        if args.seed_outcomes:lab.add_seeds('reviewed',read_json(args.seed_outcomes))
        if args.config:
            config=read_json(args.config)
            if 'rules_file' in config:
                if 'rules' in config:raise ValueError('Specify rules or rules_file, not both')
                rule_path=Path(__file__).with_name('meme_live_rules.json') if config['rules_file']=='bundled' else Path(config['rules_file'])
                config['rules']=read_json(rule_path)
            common={k:config[k] for k in ('rules','initial','capital','start_at')}
            common.update(seed_set=config.get('seed_set','reviewed'),training_cutoff=config.get('training_cutoff',config['start_at']-1))
            if common['training_cutoff']>=common['start_at']:raise ValueError('Training cutoff must precede out-of-sample replay')
            lab.register('baseline',dict(common,challengers={}))
            for scenario in config.get('scenarios',[]):
                lab.register(scenario['id'],dict(common,family=scenario['family'],challengers=scenario['challengers']))
            for grid in config.get('grids',[]):
                parameters=grid['parameters'];keys=sorted(parameters)
                if len(keys)>8 or any(not 1<=len(parameters[k])<=128 for k in keys):raise ValueError('Grid dimensions exceed budget')
                for index,values in enumerate(itertools.product(*(parameters[k] for k in keys))):
                    if index>=10000:raise ValueError('Grid exceeds 10000 scenarios')
                    settings=dict(grid.get('enabled',{}),**dict(zip(keys,values)))
                    lab.register(grid['family']+'-'+str(index),dict(common,family=grid['family'],challengers=settings))
            if args.paper_db:
                from meme_dip import DipRules
                imported=lab.ingest_paper(args.paper_db,DipRules(**common['rules']))
        elif args.paper_db:parser.error('--paper-db requires reviewed --config rules')
        if args.resume_scenario:
            with lab.db:
                lab.db.execute("UPDATE scenarios SET status='active',error=NULL WHERE id=? AND lease IS NULL",(args.resume_scenario,))
        if args.run:
            context=multiprocessing.get_context('spawn')
            processes=[context.Process(target=worker,args=(str(args.db),args.jobs,args.event_batch,args.rss_limit_mb*1_000_000)) for _ in range(args.workers)]
            for process in processes:process.start()
            for process in processes:process.join()
            if any(p.exitcode for p in processes):raise RuntimeError('A bounded worker stopped; inspect worker_metrics and retained leases')
        result={'import':imported} if args.paper_db else {}
        if args.nominate:result['nomination']=lab.nominate(args.nominate)
        if args.evaluate:
            result['evaluation']=lab.evaluate(args.evaluate)
        if args.transition:
            if not all((args.family,args.evaluate,args.evaluation_id)):parser.error('Transition requires family, candidate (--evaluate) and evaluation-id')
            result['transition']=Registry(lab.db).transition(args.family,args.evaluate,args.evaluation_id,args.transition)
        result['scenarios']=[dict(id=r[0],cursor=r[1],status=r[2],error=r[3]) for r in lab.db.execute('SELECT id,cursor,status,error FROM scenarios ORDER BY id LIMIT ?',(min(1000,max(1,args.list_limit)),))]
        result['counts']={r[0]:r[1] for r in lab.db.execute('SELECT status,count(*) FROM scenarios GROUP BY status')}
        result['peak_worker_rss_bytes']=lab.db.execute("SELECT max(json_extract(body,'$.peak_rss_bytes')) FROM worker_metrics").fetchone()[0]
        result['live_eligible']=False
        print(json.dumps(result,allow_nan=False))
    finally:lab.close()


if __name__=='__main__':main()
