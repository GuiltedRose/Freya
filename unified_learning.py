
"""Unified SVM, LSTM and retrieval candidate training and evaluation.

Offline training, frozen candidate artifacts, chronological testing,
prospective forecast capture, and evidence-backed qualification.

Does not authorize orders or alter the incumbent trading policy.
"""
import copy
import json
import math
import time

from neural_autonomy import MIN_TRAINING, MAX_AGE, qualification
from neural_network import initialize, fit_balanced, forward
from shadow_ml_models import (
    fit_svm, calibrate_svm, fit_lstm, predict, MAX_STEPS
)
from unified_observations import (
    canonical, digest, eligible, tensor, number
)


def grouped_split(records):
    """Chronological group-purged train/validation/test split."""
    positions = {}
    groups = {}
    for row in records:
        position = row["position_id"]
        group = row["group_id"]
        if position in positions and positions[position] != group:
            raise ValueError("Position correlation group changed")
        positions[position] = group
        groups.setdefault(group, []).append(row)

    ordered = sorted(
        groups.values(),
        key=lambda rows: (
            min(r["feature_at"] for r in rows),
            rows[0]["group_id"]
        )
    )
    first = int(len(ordered) * 0.6)
    second = int(len(ordered) * 0.8)

    def flatten(items):
        return [row for group in items for row in group]

    train = flatten(ordered[:first])

    def latest_known(rows):
        return max(
            (
                max(
                    row["known_at"],
                    row.get("label_known_at") or 0,
                    row.get("risk_label_at") or 0,
                )
                for row in rows
            ),
            default=0,
        )

    cutoff = latest_known(train)
    validation = flatten([group for group in ordered[first:second] if min(row['feature_at'] for row in group)>cutoff])
    cutoff = max(cutoff, latest_known(validation))
    test = flatten([group for group in ordered[second:] if min(row['feature_at'] for row in group)>cutoff])
    return train, validation, test


def windows(records,continuation=False):
    """Produce bounded, nonoverlapping chronological LSTM windows."""
    groups = {}
    for row in records:
        groups.setdefault(row["position_id"], []).append(row)

    result = []
    for position, items in groups.items():
        ordered = sorted(
            items, key=lambda row: (row["feature_at"], row["identity"])
        )
        labels = {
            r["net_pnl_usd"]
            for r in ordered if r.get("net_pnl_usd") is not None
        }
        if len(labels) > 1:
            raise ValueError("Conflicting completed position outcomes")

        for end in range(MAX_STEPS, len(ordered) + 1, MAX_STEPS):
            sequence = ordered[end - MAX_STEPS:end]
            timestamps = [r["feature_at"] for r in sequence]
            if any(
                a >= b for a, b in zip(timestamps, timestamps[1:])
            ):
                continue
            last = sequence[-1]
            if continuation:
                label=last.get('continuation_label')
                known=last.get('continuation_known_at')
                if type(label) is not int or label not in (0,1) or not number(known) or known<=last['feature_at']:continue
                result.append(([tensor(row) for row in sequence],label,dict(last,
                    position_id=last['position_id']+':continuation:'+last['identity'],net_pnl_usd=None)))
                continue
            if (
                last.get("net_pnl_usd") is None
                or last.get("net_of_costs_verified") is not True
                or not number(last.get("label_known_at"))
                or last["label_known_at"] <= last["feature_at"]
            ):
                continue
            result.append((
                [tensor(row) for row in sequence],
                int(last["net_pnl_usd"] > 0),
                last,
            ))
    return result


def metrics(predictions):
    """Position-aggregated classification and observed-outcome statistics."""
    grouped = {}
    for probability, label, row in predictions:
        if not number(probability) or not 0 <= probability <= 1:
            raise ValueError("Invalid prediction probability")
        grouped.setdefault(row["position_id"], []).append(
            (probability, label, row)
        )

    if not grouped:
        return {
            "positions": 0,
            "windows": 0,
            "brier": None,
            "accuracy": None,
            "estimated_selected_net_usd": None,
            "max_net_drawdown_usd": None,
        }

    aggregated = []
    for values in grouped.values():
        if len({value[1] for value in values}) != 1:
            raise ValueError("Conflicting targets for position")
        probability = sum(v[0] for v in values) / len(values)
        aggregated.append((probability, values[0][1], values[0][2]))

    ordered = sorted(
        aggregated,
        key=lambda value: value[2].get("label_known_at") or 0
    )
    cumulative, peak, max_drawdown = 0.0, 0.0, 0.0
    for probability, label, row in ordered:
        if probability >= 2 / 3 and number(row.get("net_pnl_usd")):
            cumulative += row["net_pnl_usd"]
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    calibration=[]
    for low in (0.,.2,.4,.6,.8):
        bucket=[(p,y) for p,y,_ in aggregated if low<=p and (p<low+.2 or low==.8)]
        if not bucket:continue
        n=len(bucket);rate=sum(y for _,y in bucket)/n;z=1.96
        center=(rate+z*z/(2*n))/(1+z*z/n)
        spread=z*math.sqrt(rate*(1-rate)/n+z*z/(4*n*n))/(1+z*z/n)
        calibration.append(dict(lower=low,count=n,predicted=sum(p for p,_ in bucket)/n,
            observed=rate,observed_interval=[max(0.,center-spread),min(1.,center+spread)]))

    return {
        "positions": len(aggregated),
        "windows": len(predictions),
        'calibration':calibration,
        "brier": sum(
            (probability - label) ** 2
            for probability, label, _ in aggregated
        ) / len(aggregated),
        "accuracy": sum(
            int((probability >= 0.5) == bool(label))
            for probability, label, _ in aggregated
        ) / len(aggregated),
        "estimated_selected_net_usd": cumulative,
        "max_net_drawdown_usd": max_drawdown,
        "scope": (
            "Observed position outcomes only; not a simulated "
            "or executable counterfactual trading result"
        ),
    }


def build_candidate(store, family, now=None, risk_config=None, features_config=None):
    """Build or reuse a frozen candidate from versioned eligible evidence."""
    if family not in ("svm", "lstm", "rag"):
        raise ValueError("Unknown model family")

    now = time.time() if now is None else now
    from capital_policy import CapitalConfig,asdict
    risk_config=risk_config or CapitalConfig()
    features_config=features_config or {}
    advanced_risk=family=='svm' and features_config.get('svm_risk_v2') is True
    continuation=family=='lstm' and features_config.get('lstm_continuation') is True
    records = [
        row for row in store.observations()
        if eligible(row)
        and row["feature_at"] <= now
        and row["known_at"] <= now
        and max(
            row.get("label_known_at") or 0,
            row.get("risk_label_at") or 0,
        ) <= now
    ]

    fingerprint = digest([
        "unified-v5-qualified-context", family, features_config, asdict(risk_config), [digest(row) for row in records]
    ])
    with store.connect(readonly=True) as db:
        old = db.execute(
            "SELECT body FROM models WHERE fingerprint=?",
            (fingerprint,)
        ).fetchone()
    if old:
        return json.loads(old[0])

    if not records:
        return {
            "status": "blocked",
            "reason": "No eligible verified learning observations",
        }

    schemas = {row["feature_schema"] for row in records}
    sizes = {len(tensor(row)) for row in records}
    if len(schemas) != 1 or len(sizes) != 1:
        raise ValueError("Mixed feature schemas or feature widths")

    train, validation, test = grouped_split(records)
    model = None
    report = {}

    if family == "svm":
        from market_analysis import verified_risk_label,risk_tensor
        def inputs(row):return risk_tensor(row) if advanced_risk else tensor(row)
        def valid_risk(rows):
            candidates = [
                row for row in rows
                if row.get("risk_label_verified") is True
                and row.get("risk_label") in (0, 1)
                and number(row.get("risk_label_at"))
                and row["risk_label_at"] >= row["feature_at"]
                and (not advanced_risk or verified_risk_label(row) and row.get('risk_features') is not None)
            ]
            valid=[]
            for row in candidates:
                try:inputs(row)
                except (ValueError,TypeError):continue
                valid.append(row)
            return valid

        training = valid_risk(train)
        calibration = valid_risk(validation)
        testing = valid_risk(test)

        if (
            len({r["group_id"] for r in training}) < MIN_TRAINING
            or len({r["group_id"] for r in calibration}) < 10
            or len({r["risk_label"] for r in training}) < 2
            or len({r["risk_label"] for r in calibration}) < 2
        ):
            return {
                "status": "blocked",
                "reason": (
                    "Verified two-class risk labels and independent "
                    "calibration observations required"
                ),
            }

        model = fit_svm([
            (inputs(row), row["risk_label"]) for row in training
        ])
        model = calibrate_svm(model, [
            (inputs(row), row["risk_label"]) for row in calibration
        ])
        report = metrics([
            (predict(model, inputs(row)), row["risk_label"], row)
            for row in testing
        ])
        report.pop("estimated_selected_net_usd", None)
        report.pop("max_net_drawdown_usd", None)
        report["target"] = "verified_token_risk"
        model['input_kind']='risk_v2' if advanced_risk else 'legacy'

        rules = [
            row for row in testing
            if number(row.get("rule_risk_score"))
            and 0 <= row["rule_risk_score"] <= 1
        ]
        report["rule_baseline"] = metrics([
            (row["rule_risk_score"], row["risk_label"], row)
            for row in rules
        ])
        lineage = training + calibration

    elif family == "lstm":
        training = windows(train,continuation=continuation)
        testing = windows(test,continuation=continuation)

        if (
            len({row["group_id"] for _, _, row in training})
            < MIN_TRAINING
        ):
            return {
                "status": "blocked",
                "reason": (
                    "Insufficient independent verified "
                    "chronological LSTM windows"
                ),
            }

        model = fit_lstm([
            (sequence, label)
            for sequence, label, _ in training
        ])
        if continuation:
            calibration=windows(validation,continuation=True)
            if len({r['group_id'] for _,_,r in calibration})<10 or len({y for _,y,_ in calibration})<2:
                return {'status':'blocked','reason':'Independent two-class continuation calibration windows required'}
            calibrator=calibrate_svm({'weights':[1.], 'bias':0.},
                [([predict(model,sequence)],label) for sequence,label,_ in calibration])
            model['probability_calibration']=calibrator['calibration']

        baseline = fit_balanced(
            initialize(len(training[0][0][-1])),
            [
                (sequence[-1], label)
                for sequence, label, _ in training
            ],
        )
        report = metrics([
            (predict(model, sequence), label, row)
            for sequence, label, row in testing
        ])
        report["feedforward_baseline"] = metrics([
            (forward(baseline, sequence[-1])[0], label, row)
            for sequence, label, row in testing
        ])
        prior=sum(label for _,label,_ in training)/len(training)
        report['prior_baseline']=metrics([(prior,label,row) for _,label,row in testing])
        from meme_learning import chart_features
        def momentum_bin(row):
            values=chart_features(row.get('original_context') or {})
            return int(values[0]>0) if any(values) else None
        buckets={key:[label for _,label,row in training if momentum_bin(row)==key] for key in (0,1)}
        def momentum_probability(row):
            values=buckets.get(momentum_bin(row),[])
            return sum(values)/len(values) if len(values)>=5 else prior
        report['momentum_baseline']=metrics([(momentum_probability(row),label,row) for _,label,row in testing])
        baselines=(report['prior_baseline'],report['feedforward_baseline'],report['momentum_baseline'])
        report['predictive_contribution_verified']=bool(report['positions']>=MIN_TRAINING and report['brier'] is not None
            and all(b['brier'] is not None and report['brier']<b['brier'] for b in baselines))
        report['contribution_scope']='held_out_predictive_loss; executable improvement and prospective calibration unproven'
        model['task']='net_quote_continuation' if continuation else 'completed_position_outcome'
        model['training_prior']=prior
        lineage = train + validation

    else:
        training = [
            row for row in train
            if row.get("net_pnl_usd") is not None
            and row.get("net_of_costs_verified") is True
        ]
        if len({r["group_id"] for r in training}) < MIN_TRAINING:
            return {
                "status": "blocked",
                "reason": "Insufficient independent retrieval outcomes",
            }

        training = training[:512]
        model = {
            "family": "rag",
            'risk_config':asdict(risk_config),
            'lifecycle_context':features_config.get('lifecycle_context',False),
            "corpus_ids": [
                digest([row["source"], row["identity"]])
                for row in training
            ],
        }
        predictions = []
        comparisons=[]
        calibration_rows=[]
        for row in validation+test:
            if (
                row.get("net_pnl_usd") is None
                or row.get("net_of_costs_verified") is not True
            ):
                continue
            result = store.retrieve(
                tensor(row),
                row["feature_at"],
                row["group_id"],
                allow_ids=set(model["corpus_ids"]),
                feature_schema_id=next(iter(schemas)),
                risk_config=risk_config,
                exclude_mint=row['mint'],
                lifecycle_regime=(row.get('lifecycle') or {}).get('regime') if features_config.get('lifecycle_context') else None,
            )
            if len(result["neighbors"]) >= 3:
                item=dict(id=digest([row['source'],row['identity']]),group_id=row['group_id'],exit_at=row['label_known_at'],
                    pnl=row['net_pnl_usd'],selected=result['edge_supported'],probability=result['score'])
                (calibration_rows if row in validation else comparisons).append(item)
                if row in test:predictions.append((result['score'],int(row['net_pnl_usd']>0),row))
        report = metrics(predictions)
        from expectancy_validation import compare
        report['validation_net']=compare(calibration_rows,risk_config.min_samples,risk_config.bootstrap_samples)
        report['test_net']=compare(comparisons,risk_config.min_samples,risk_config.bootstrap_samples)
        report["retrieval_scope"] = (
            "Frozen historical corpus, decision-time eligible labels"
        )
        lineage = train

    identifiers = [
        digest([row["source"], row["identity"]])
        for row in lineage
    ]
    fingerprints = {
        digest([row["source"], row["identity"]]): digest(row)
        for row in lineage
    }

    artifact = {
        "family": family,
        "version": "unified-v5-qualified-context",
        'features_config':features_config,
        "model": model,
        "created_at": now,
        "training_ids": identifiers,
        "training_fingerprints": fingerprints,
        "feature_schema": next(iter(schemas)),
        "feature_size": next(iter(sizes)),
        "historical_evaluation": report,
        "status": "collecting",
        "live_eligible": False,
    }
    artifact["id"] = digest(artifact)
    with store.connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO models VALUES(?,?,?)",
            (artifact["id"], fingerprint, canonical(artifact)),
        )
    return artifact


def load_candidate(store, identifier):
    with store.connect(readonly=True) as db:
        row = db.execute(
            "SELECT body FROM models WHERE id=?", (identifier,)
        ).fetchone()
    if not row:
        raise ValueError("Unknown candidate ID")
    candidate = json.loads(row[0])
    content = {k: v for k, v in candidate.items() if k != "id"}
    if digest(content) != identifier:
        raise ValueError("Frozen candidate integrity mismatch")
    return candidate


def lineage_valid(store, candidate):
    actual = {
        digest([row["source"], row["identity"]]): digest(row)
        for row in store.by_ids(candidate["training_ids"])
        if eligible(row)
    }
    return actual == candidate["training_fingerprints"]


def forecast(store, identifier, observations, position_id, context_kind='offline'):
    """Persist a real, timestamped prediction before later evaluation."""
    if context_kind not in ('offline','paper','live'):raise ValueError('Unknown forecast context')
    candidate = load_candidate(store, identifier)
    now = time.time()
    if (
        not observations
        or len(observations) > MAX_STEPS
        or any(
            not eligible(row)
            or row["feature_at"] > now
            or row["known_at"] > now
            for row in observations
        )
    ):
        return {
            "score": None,
            "reason": "Verified inference observations required",
            "market_eligible": False,
        }
    if not lineage_valid(store, candidate):
        return {
            "score": None,
            "reason": "Frozen training evidence revoked or changed",
            "market_eligible": False,
        }

    ordered = sorted(observations, key=lambda r: r["feature_at"])
    if (
        any(
            a["feature_at"] >= b["feature_at"]
            for a, b in zip(ordered, ordered[1:])
        )
        or any(
            row["position_id"] != position_id
            or row["feature_schema"] != candidate["feature_schema"]
            for row in ordered
        )
        or len({row["mint"] for row in ordered}) != 1
    ):
        raise ValueError("Invalid inference chronology/schema/position")

    features = tensor(ordered[-1])
    family = candidate["family"]
    if family=='svm' and candidate['model'].get('input_kind')=='risk_v2':
        from market_analysis import risk_tensor
        features=risk_tensor(ordered[-1])
    neighbors = []
    retrieval=None

    if family == "svm":
        # SVM is trained on risky=1, so return P(safe).
        score = 1.0 - predict(candidate["model"], features)
    elif family == "lstm":
        if len(ordered) != MAX_STEPS:
            return {
                "score": None,
                "reason": "Complete chronological LSTM window required",
                "market_eligible": False,
            }
        score = predict(
            candidate["model"], [tensor(row) for row in ordered]
        )
    elif family == "rag":
        from capital_policy import CapitalConfig
        result = store.retrieve(
            features,
            ordered[-1]["feature_at"],
            ordered[-1]["group_id"],
            allow_ids=set(candidate["model"]["corpus_ids"]),
            feature_schema_id=candidate["feature_schema"],
            risk_config=CapitalConfig(**candidate['model'].get('risk_config',{})),
            exclude_mint=ordered[-1]['mint'],
            lifecycle_regime=(ordered[-1].get('lifecycle') or {}).get('regime') if candidate['model'].get('lifecycle_context') else None,
        )
        neighbors = result["neighbors"]
        retrieval={k:v for k,v in result.items() if k!='neighbors'}
        score = result["score"] if len(neighbors) >= 3 else None
    else:
        raise ValueError("Unsupported candidate family")

    prediction = {
        "candidate_id": identifier,
        "score": score,
        "recorded_at": now,
        "position_id": position_id,
        'context_kind':context_kind,'mint':ordered[-1]['mint'],'feature_at':ordered[-1]['feature_at'],
        "evidence_ids": [row["identity"] for row in ordered],
        "neighbors": neighbors,
        'retrieval':retrieval,
        "market_eligible": False,
    }
    if score is not None:
        with store.connect() as db:
            existing = db.execute(
                "SELECT 1 FROM forecasts "
                "WHERE candidate_id=? AND position_id=?",
                (identifier, position_id)
            ).fetchone()
            if existing:
                raise ValueError(
                    "Forecast already frozen for this candidate/position"
                )
            db.execute(
                "INSERT INTO forecasts VALUES(?,?,?,?)",
                (identifier, position_id, now, canonical(prediction)),
            )
    return prediction


def evaluate_candidate(store, identifier, outcomes, failures=()):
    """Qualify prospective confirmed results; never authorizes an order itself."""
    candidate = load_candidate(store, identifier)
    if not lineage_valid(store, candidate) or not 0 <= time.time()-candidate['created_at'] <= MAX_AGE:
        return {
            "status": "blocked",
            "reason": "Frozen training evidence unavailable",
            "live_eligible": False,
        }

    with store.connect(readonly=True) as db:
        predictions = {
            position: json.loads(body)
            for position, body in db.execute(
                "SELECT position_id,body FROM forecasts "
                "WHERE candidate_id=?", (identifier,)
            )
        }

    from data_policy import require_learning_rights, SourceAccessBlocked

    verified = []
    for original in outcomes:
        if (
            original.get("source") != "bot_live"
            or original.get("provenance", {}).get("verified") is not True
            or original.get("rights", {}).get("evaluation_verified") is not True
            or original.get("net_of_costs_verified") is not True
        ):
            continue
        try:
            require_learning_rights(original)
        except SourceAccessBlocked:
            continue
        prediction = predictions.get(original.get("position_id"))
        context = original.get("entry_context") or {}
        if (
            not prediction
            or prediction.get("score") is None
            or context.get("recorded_before_order") is not True
            or prediction["recorded_at"] > original.get("entry_at", 0)
        ):
            continue

        row = copy.deepcopy(original)
        row.setdefault("entry_context", {})["neural_selection"] = {
            "candidate_id": identifier,
            "score": 1-prediction["score"] if candidate['family']=='svm' else prediction["score"],
        }
        verified.append(row)

    # Existing qualification requires an envelope with an immutable ID.
    result = qualification(candidate, verified, failures)
    if candidate.get('model',{}).get('task')=='net_quote_continuation':
        from neural_autonomy import MIN_VALIDATION,MIN_DAYS
        with store.connect(readonly=True) as db:
            saved=db.execute("SELECT body FROM observations WHERE json_extract(body,'$.continuation_label') IS NOT NULL AND id NOT IN (SELECT id FROM revocations) ORDER BY feature_at LIMIT 4097").fetchall()
        labeled=[];seen_groups=set();seen_positions=set()
        if len(saved)>4096:saved=[]  # Explicit paged review needed beyond this bound.
        for (body,) in saved:
            row=json.loads(body);position=row.get('model_position_id');prediction=predictions.get(position)
            known=row.get('continuation_known_at');at=row['feature_at']
            if (not prediction or prediction.get('context_kind')!='live' or prediction.get('score') is None or not eligible(row)
                or row.get('source')!='bot_live' or row.get('net_of_costs_verified') is not True
                or row.get('provenance',{}).get('quote_timestamp_verified') is not True
                or not number(known) or not 60<=known-at<=180 or known>time.time()
                or not candidate['created_at']<prediction['recorded_at']<=at<=prediction['recorded_at']+30
                or row['group_id'] in seen_groups or position in seen_positions):continue
            seen_groups.add(row['group_id']);seen_positions.add(position)
            labeled.append((prediction['score'],row['continuation_label'],dict(row,position_id=position,net_pnl_usd=None)))
        report=metrics(labeled)
        prior=candidate['model'].get('training_prior')
        baseline=metrics([(prior,label,row) for _,label,row in labeled]) if number(prior) else {'brier':None}
        days=len({int(row['continuation_known_at']//86400) for _,_,row in labeled})
        passed=(len(labeled)>=MIN_VALIDATION and days>=MIN_DAYS and len({label for _,label,_ in labeled})==2
            and candidate['historical_evaluation'].get('predictive_contribution_verified') is True
            and report['brier'] is not None and baseline['brier'] is not None and report['brier']<baseline['brier'])
        result.update(continuation_evaluation=report,continuation_prior_baseline=baseline,continuation_days=days,
                      continuation_horizon_seconds=[60,180],continuation_reference_lag_seconds=[0,30])
        if not passed:
            result.update(status='collecting' if len(labeled)<MIN_VALIDATION or days<MIN_DAYS else 'rejected',
                          reason='Independent prospective quote-horizon evidence and predictive improvement required')
    if candidate['family']=='svm':
        from market_analysis import verified_risk_label
        from neural_autonomy import MIN_VALIDATION,MIN_DAYS
        annotations=[dict(row,feature_at=row['entry_at']) for row in verified
                     if predictions[row['position_id']].get('context_kind')=='live']
        with store.connect(readonly=True) as db:
            saved=db.execute("SELECT body FROM observations WHERE json_extract(body,'$.risk_label_verified')=1 AND id NOT IN (SELECT id FROM revocations) ORDER BY feature_at LIMIT 4097").fetchall()
        if len(saved)<=4096:
            for (body,) in saved:
                row=json.loads(body)
                position=row.get('model_position_id') or (row.get('original_context') or {}).get('model_position_id') or row['position_id']
                prediction=predictions.get(position)
                if (not prediction or prediction.get('context_kind')!='live' or not eligible(row)
                    or prediction.get('mint')!=row['mint'] or not candidate['created_at']<prediction['recorded_at']
                    or row['feature_at']>prediction['recorded_at'] or not number(row.get('risk_label_at'))
                    or row['risk_label_at']<=prediction['recorded_at']):continue
                annotations.append(dict(row,position_id=position))
        annotations=[row for row in annotations if verified_risk_label(row) and row['risk_label_at']<=time.time()]
        unique={}
        for row in sorted(annotations,key=lambda row:row['risk_label_at']):
            unique.setdefault(row.get('group_id',row['mint']),row)
        annotations=list(unique.values())
        scores=metrics([(predictions[row['position_id']]['score'],row['risk_label'],row) for row in annotations])
        labels=[row['risk_label'] for row in annotations]
        prior=sum(labels)/len(labels) if labels else 0.
        prior_brier=prior*(1-prior)
        days=len({int(row['risk_label_at']//86400) for row in annotations})
        independent=len({row.get('group_id',row['mint']) for row in annotations})
        risk_passed=(independent>=MIN_VALIDATION and days>=MIN_DAYS and len(set(labels))==2
                     and scores['brier'] is not None and scores['brier']<prior_brier)
        result.update(risk_classification=scores,independent_risk_groups=independent,
                      observed_risk_days=days,constant_prior_brier=prior_brier)
        if not risk_passed:
            result.update(status='collecting' if independent<MIN_VALIDATION or days<MIN_DAYS else 'rejected',
                          reason='Independent prospective risk annotations and calibrated predictive improvement required')
    if candidate['family']=='rag':
        from expectancy_validation import compare
        rows=[]
        for row in verified:
            prediction=predictions[row['position_id']]
            rows.append(dict(id=row['exit_signature'],group_id=row.get('group_id',row['mint']),exit_at=row['exit_at'],
                pnl=row['net_pnl_usd'],selected=(prediction.get('retrieval') or {}).get('edge_supported',False),probability=prediction['score']))
        comparison=compare(rows)
        days=len({int(row['exit_at']//86400) for row in verified})
        from neural_autonomy import MIN_DAYS
        # General qualification charges failed orders; retrieval must also retain that veto.
        fee_valid=all(number(f.get('network_cost_usd')) and f['network_cost_usd']>=0 for f in failures)
        fee_total=sum(f['network_cost_usd'] for f in failures) if fee_valid else float('inf')
        history=candidate['historical_evaluation'].get('test_net',{})
        passed=(comparison['status']=='promotable' and history.get('status')=='promotable' and days>=MIN_DAYS
                and fee_valid and comparison['policy_net']-fee_total>comparison['baseline_net']
                and comparison['improvement_lower']>fee_total/max(1,comparison['groups']))
        result=dict(status='qualified' if passed else 'collecting' if days<MIN_DAYS or comparison['status']=='collecting' else 'rejected',
                    net_comparison=comparison,failed_order_cost_usd=fee_total if fee_valid else None,observed_days=days)
    result.update({
        "live_eligible": result.get('status') == 'qualified',
        "promotion_history_unchanged": True,
        "evaluation_scope": "offline_prospective_review",
    })

    snapshot = {
        "candidate_id": identifier,
        "qualification": result,
        "evaluated_at": time.time(),
    }
    key = digest([identifier, result])
    with store.connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO evaluations VALUES(?,?,?)",
            (key, identifier, canonical(snapshot)),
        )
    return result
