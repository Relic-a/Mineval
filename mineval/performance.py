"""Descriptive performance with explicit samples and exact comparable intersections."""
from collections import defaultdict
import json
from .artifacts import read_json
from .categories import CATEGORIES, LEGACY
from .memory import encoded
from .workflows import tasks
from .publication import published
from .benchmark import condition_key, conditions

VERDICTS = ('pass', 'fail', 'inconclusive', 'error')


def sample_key(record, run=None):
    if run is not None:
        return condition_key(record, run)
    return encoded([record['prepared_revision'], record['policy'], record['config_sha256'],
                    record['verification_sha256'], record.get('image_id')])


def counts(records, total, sample_total):
    result = {v: sum(r['verdict'] == v for r in records) for v in VERDICTS}
    n = len(records)
    covered = len({r['task_id'] for r in records})
    return result | {'evaluated': n, 'attempt_count': n, 'infrastructure_errors': result['error'],
                     'available': total, 'pass_rate': result['pass'] / (result['pass'] + result['fail'])
                     if result['pass'] + result['fail'] else None,
                     'covered_tasks': covered, 'coverage': covered / total if total else None,
                     'available_samples': sample_total,
                     'sample_coverage': len({sample_key(r) for r in records}) / sample_total if sample_total else None,
                     'decided': result['pass'] + result['fail']}


def summaries(store):
    pool = tasks(store)
    categories = {t['task_id']: t['category'] for t in pool}
    available = {c: sum(t['category'] == c and bool(t['versions']) for t in pool) for c in (*CATEGORIES, LEGACY)}
    groups = defaultdict(dict)
    warnings = []
    # Count each attempt once using its first published evaluation.
    entries = []
    for task in pool:
        tid = task['task_id']
        for path in (store.task(tid) / 'evaluations').glob('*.json'):
            try:
                ev = store.inspect_evaluation(tid, path.stem)
                if ev.get('schema_version') != 2 or not published(store, tid, ev['run_id'], ev['evaluation_id']):
                    continue
                run = store.inspect_run(tid, ev['run_id'])
                entries.append((run.get('started_at', ''), ev.get('evaluation_version', 0), ev['evaluation_id'], run, ev))
            except (ValueError, OSError, KeyError):
                warnings.append(f'{tid}/{path.stem}: unavailable or busy; refresh or inspect receipts')
    seen = set()
    for _, _, _, run, ev in sorted(entries, key=lambda x: x[:3]):
        attempt = (ev['task_id'], run['run_id'])
        if attempt in seen:
            continue
        seen.add(attempt)
        operation = conditions(run)
        budget = {k: operation[k] for k in ('limits', 'network') if k in operation}
        budget['grading'] = ev.get('grading_conditions')
        identity = encoded([run['model'], run['harness'], budget])
        groups[identity].setdefault(sample_key(ev, run), []).append(
            ev | {'category': categories[ev['task_id']]})
    cohort_models = defaultdict(list)
    for identity in groups:
        _, harness, budget = json.loads(identity)
        cohort_models[encoded([harness, budget])].append(identity)
    common_count = 0
    results = []
    for identities in cohort_models.values():
        matched = set.intersection(*(set(groups[key]) for key in identities)) if len(identities) > 1 else set()
        common_count += len(matched)
        pool = {key: records[0] for identity in identities for key, records in groups[identity].items()}
        for identity in identities:
            model, harness, budget = json.loads(identity)
            records = groups[identity]
            all_attempts = [r for attempts in records.values() for r in attempts]
            comparable = [r for key in sorted(matched) for r in records[key]]
            unmatched = [r for key in records.keys() - matched for r in records[key]]
            breakdown = {c: counts([r for r in all_attempts if r['category'] == c], available[c],
                                    sum(r['category'] == c for r in pool.values())) for c in available}
            rates = [m['pass_rate'] for c, m in breakdown.items() if c != LEGACY and m['pass_rate'] is not None]
            overall = counts(all_attempts, sum(available.values()), len(pool))
            overall['sample_coverage'] = len(records) / len(pool)
            results.append({'model': model, 'harness': harness, 'conditions': budget, 'overall': overall,
                'categories': breakdown, 'category_average': sum(rates) / len(rates) if rates else None,
                'category_count': len(rates),
                'comparable': counts(comparable, len({r['task_id'] for r in comparable}), len(matched)),
                'comparable_categories': {c: counts([r for r in comparable if r['category'] == c],
                                                  len({r['task_id'] for r in comparable if r['category'] == c}),
                                                  sum(pool[key]['category'] == c for key in matched)) for c in available},
                'selections': [{'task_id': r['task_id'], 'evaluation_id': r['evaluation_id']} for r in all_attempts],
                'common_selections': [{'task_id': r['task_id'], 'evaluation_id': r['evaluation_id']} for r in comparable],
                'excluded': [{'task_id': r['task_id'], 'run_id': r['run_id'],
                              'reason': 'no matching task revision and conditions across models'} for r in unmatched]})
    return {'models': results, 'warnings': warnings, 'common_samples': common_count}
