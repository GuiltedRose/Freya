
"""Command-line runner for unified SVM, LSTM and historical retrieval.

Runs without network access or wallet permissions.
Does not submit transactions.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from unified_observations import ObservationStore
from unified_learning import (
    build_candidate,
    load_candidate,
    forecast,
    evaluate_candidate,
)


MAX_INPUT_BYTES = 1_000_000
MAX_IMPORT_ROWS = 1024


def load_json(path):
    with Path(path).open("rb") as stream:
        content = stream.read(MAX_INPUT_BYTES + 1)
    if len(content) > MAX_INPUT_BYTES:
        raise ValueError("Input file too large")
    return json.loads(content)


def import_jsonl(store, path):
    count = 0
    with Path(path).open("rb") as stream:
        for line in stream:
            if not line.strip():
                continue
            if len(line) > 65536 or count >= MAX_IMPORT_ROWS:
                raise ValueError("Import batch limit exceeded")
            row = json.loads(line)
            store.add(row)
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--pipeline-config", type=Path, help="Reviewed ledger import and bounded family training")
    parser.add_argument('--sizing-evaluation-input',type=Path)
    parser.add_argument('--capital-config',type=Path,default=Path(__file__).with_name('capital_policy.example.json'))
    parser.add_argument('--loop',action='store_true',help='Periodic bounded learning batches, with one writer per model database')
    parser.add_argument('--interval',type=int,default=900)
    parser.add_argument("--status", action="store_true")
    parser.add_argument(
        "--train", choices=("svm", "lstm", "rag", "all")
    )
    parser.add_argument("--candidate")
    parser.add_argument("--forecast-input", type=Path)
    parser.add_argument("--evaluation-input", type=Path)
    args = parser.parse_args()

    if args.loop:
        if any((args.input,args.sizing_evaluation_input,args.status,args.train,args.candidate,args.forecast_input,args.evaluation_input)):
            parser.error('--loop cannot combine one-shot import, training, forecast or evaluation flags')
        if not args.pipeline_config or not 30<=args.interval<=86400:
            parser.error('--loop requires --pipeline-config and interval 30..86400')
        from pipeline_runtime import read_json,writer_lock
        command=['--db',str(args.db),
                 '--pipeline-config',str(args.pipeline_config),'--capital-config',str(args.capital_config)]
        ownership={'pipeline':read_json(args.pipeline_config),'capital':read_json(args.capital_config)}
        with writer_lock(args.db,'learning',ownership):
            while True:
                current=command+([] if args.db.exists() else ['--initialize'])
                previous=sys.argv
                try:
                    sys.argv=[previous[0],*current]
                    main()
                finally:sys.argv=previous
                time.sleep(args.interval)
        return

    store = ObservationStore(args.db, create=args.initialize)
    imported = import_jsonl(store, args.input) if args.input else 0

    report = {
        "imported": imported,
        "models": {},
        "order_authorization": False,
    }
    if args.pipeline_config:
        from learning_pipeline import load_config, synchronize
        from capital_policy import load_config as load_capital_config
        report['pipeline'] = synchronize(store,load_config(args.pipeline_config),load_capital_config(args.capital_config))
    if args.sizing_evaluation_input:
        from capital_policy import load_config as capital_config,canonical
        from expectancy_validation import compare_sizing
        from unified_observations import digest
        payload=load_json(args.sizing_evaluation_input)
        rows=payload['outcomes']
        if len(rows)>1024:raise ValueError('Sizing comparison exceeds bounded batch')
        report['sizing_comparison']=compare_sizing(rows,capital_config(args.capital_config))
        key=digest([rows,report['sizing_comparison']])
        with store.connect() as db:
            db.execute('INSERT OR IGNORE INTO evaluations VALUES(?,?,?)',(key,'capital-policy',canonical(report['sizing_comparison'])))

    if args.train:
        families = (
            ("svm", "lstm", "rag")
            if args.train == "all" else (args.train,)
        )
        for family in families:
            from capital_policy import load_config as load_capital_config
            candidate = build_candidate(store, family,risk_config=load_capital_config(args.capital_config))
            report["models"][family] = {
                key: candidate[key]
                for key in (
                    "id", "status", "reason", "historical_evaluation"
                )
                if key in candidate
            }

    if args.status:
        with store.connect(readonly=True) as db:
            report["status"] = {
                "observations": db.execute(
                    "SELECT COUNT(*) FROM observations"
                ).fetchone()[0],
                "revocations": db.execute(
                    "SELECT COUNT(*) FROM revocations"
                ).fetchone()[0],
                "models": [
                    row[0] for row in db.execute(
                        "SELECT id FROM models ORDER BY id LIMIT 100"
                    )
                ],
            }

    if args.forecast_input:
        if not args.candidate:
            parser.error("--candidate required for forecasting")
        payload = load_json(args.forecast_input)
        report["forecast"] = forecast(
            store, args.candidate,
            payload["observations"], payload["position_id"]
        )

    if args.evaluation_input:
        if not args.candidate:
            parser.error("--candidate required for evaluation")
        payload = load_json(args.evaluation_input)
        outcomes = payload.get("outcomes", [])
        failures = payload.get("failures", [])
        if len(outcomes) > 1024 or len(failures) > 1024:
            raise ValueError("Evaluation batch limit exceeded")
        report["evaluation"] = evaluate_candidate(
            store, args.candidate, outcomes, failures
        )

    print(json.dumps(report, sort_keys=True, allow_nan=False),flush=True)


if __name__ == "__main__":
    try:main()
    except KeyboardInterrupt:pass
