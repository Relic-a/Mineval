"""Descriptive performance with explicit samples and exact comparable intersections."""
from collections import defaultdict
import json
from .artifacts import read_json
from .categories import CATEGORIES, LEGACY
from .memory import encoded
from .workflows import tasks
from .publication import published

VERDICTS = ('pass', 'fail', 'inconclusive', 'error')


def sample_key(record):
    return encoded([record['prepared_revision'], record['policy'], record['config_sha256'],
                    record['verification_sha256'], record.get('image_id')])


def counts(records, total, sample_total):
    result = {v: sum(r['verdict'] == v for r in records) for v in VERDICTS}
    n = len(records)
    covered = len({r['task_id'] for r in records})
    return result | {'evaluated': n, 'available': total, 'pass_rate': result['pass'] / n if n else None,
                     'covered_tasks': covered, 'coverage': covered / total if total else None,
                     'available_samples': sample_total, 'sample_coverage': n / sample_total if sample_total else None,
                     'decided': result['pass'] + result['fail']}


def summaries(store):
    pool = tasks(store)
    categories = {t['task_id']: t['category'] for t in pool}
    available = {c: sum(t['category'] == c and bool(t['versions']) for t in pool) for c in (*CATEGORIES, LEGACY)}
    groups = defaultdict(dict)
    warnings = []
    # First evaluation of earliest attempt per exact revision and evaluation condition.
    # More than one revision/condition for a task is kept in separate sample cohorts.
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
    for _, _, _, run, ev in sorted(entries, key=lambda x: x[:3]):
        identity = encoded([run['model'], run['harness']])
        groups[identity].setdefault(sample_key(ev), ev | {'category': categories[ev['task_id']]})
    common = set.intersection(*(set(records) for records in groups.values())) if groups else set()
    sample_pool = {key: record for records in groups.values() for key, record in records.items()}
    results = []
    for identity, records in groups.items():
        model, harness = json.loads(identity)
        # All-evidence figures count exact revision/condition samples, explicitly not
        # unique tasks. Coverage counts distinct tasks against prepared registrations.
        breakdown = {c: counts([r for r in records.values() if r['category'] == c], available[c],
                               sum(r['category'] == c for r in sample_pool.values())) for c in available}
        rates = [m['pass_rate'] for c, m in breakdown.items() if c != LEGACY and m['evaluated']]
        comparable = [records[k] for k in common]
        results.append({'model': model, 'harness': harness, 'overall': counts(list(records.values()), sum(available.values()), len(sample_pool)),
            'categories': breakdown, 'category_average': sum(rates) / len(rates) if rates else None,
            'category_count': len(rates), 'comparable': counts(comparable, len({r['task_id'] for r in comparable}), len(common)),
            'comparable_categories': {c: counts([r for r in comparable if r['category'] == c],
                                              len({r['task_id'] for r in comparable if r['category'] == c}),
                                              sum(records[k]['category'] == c for k in common)) for c in available},
            'selections': [{'task_id': r['task_id'], 'evaluation_id': r['evaluation_id']} for r in records.values()],
            'common_selections': [{'task_id': records[k]['task_id'], 'evaluation_id': records[k]['evaluation_id']} for k in sorted(common)]})
    return {'models': results, 'warnings': warnings, 'common_samples': len(common)}
