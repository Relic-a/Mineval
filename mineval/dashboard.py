"""Server-rendered historical operating pages, sharing observation navigation/style."""
from uuid import uuid4
import json

from .artifacts import read_json
from .categories import CATEGORIES, LEGACY
from .memory import Memory
from .observation import esc, table, status, pretty
from .performance import summaries
from .workflows import tasks, EFFORTS


def hidden(name, value):
    return '<input type="hidden" name="' + esc(name) + '" value="' + esc(value) + '">'


def form(pages, action, content):
    return '<form method="post" action="' + esc(pages.url('actions', action)) + '">' + hidden('csrf', pages.csrf) + hidden('request_id', uuid4().hex) + content + '</form>'


def select(name, values, current='', label=None):
    return '<label>' + esc(label or name.title()) + ' <select name="' + esc(name) + '">' + ''.join(
        '<option value="' + esc(value) + '"' + (' selected' if value == current else '') + '>' + esc(text) + '</option>'
        for value, text in values) + '</select></label>'


def pool(pages, query):
    rows = tasks(pages.store)
    all_rows = rows
    value = lambda key: query.get(key, [''])[0]
    for field in ('repository', 'category'):
        if value(field):
            rows = [t for t in rows if t[field] == value(field)]
    if value('status'):
        rows = [t for t in rows if t['state']['status'] == value('status')]
    if value('prepared'):
        rows = [t for t in rows if bool(t['versions']) == (value('prepared') == 'yes')]
    if value('search'):
        rows = [t for t in rows if value('search').casefold() in (t['task_summary'] + t['task_id']).casefold()]
    filters = '<form method="get"><div class="columns">'
    for field in ('repository', 'category'):
        filters += select(field, [('', 'All')] + [(x, x) for x in sorted({t[field] for t in all_rows})], value(field))
    filters += select('status', [('', 'All')] + [(x, x) for x in sorted({t['state']['status'] for t in all_rows})], value('status'))
    filters += select('prepared', [('', 'All'), ('yes', 'Prepared'), ('no', 'Unprepared')], value('prepared'), 'Preparation')
    filters += '</div><label>Search tasks <input name="search" value="' + esc(value('search')) + '"></label><button>Filter tasks</button></form>'
    body = '<p>Prepare historical tasks, run exact frozen revisions, and inspect independent evidence. ' + pages.link('Models and results', 'models') + ' · ' + pages.link('Work queue', 'jobs') + '</p>' + filters
    task_rows = []
    for t in rows:
        tid = t['task_id']
        checkbox = '<label><input type="checkbox" name="task" value="' + esc(tid) + '"> Select ' + esc(tid) + '</label>' if pages.workflows else ''
        versions = select('version_' + tid, [(v, v) for v in t['versions']], t['state'].get('version', ''), 'Revision') if t['versions'] else 'Unprepared'
        task_rows.append((checkbox + pages.task_link(tid), esc(t['repository']) + '<br>' + esc(t['category']), esc(t['task_summary']), status(t['state']['status']), versions))
    content = ('<p>No tasks match these filters. ' + pages.link('Clear filters') + '</p>' if all_rows and not rows else
               '<div class="task-pool">' + table(['Task', 'Repository / category', 'Problem', 'Status', 'Frozen revision'], task_rows) + '</div>')
    if pages.workflows:
        presets = pages.workflows.settings.get('models', [{'model': 'gpt-6-luna', 'effort': 'medium', 'adapter': 'codex'}])
        from .workflows import configuration
        choices = ''
        import json
        for item in presets:
            config = configuration(item)
            choices += '<label><input type="checkbox" name="configuration" value="' + esc(json.dumps(config)) + '"> ' + esc(config['model'] + ' · ' + config['effort'] + ' · ' + config['adapter']) + '</label>'
        content += '<h2>Run selected tasks</h2><fieldset><legend>Models and reasoning efforts</legend>' + choices
        content += '<label>Explicit model identifiers (one per line)<br><textarea name="models" rows="3" cols="40" placeholder="provider/model-name"></textarea></label>'
        content += select('effort', [(x, x) for x in EFFORTS], 'medium', 'Effort for explicit identifiers')
        content += '<label><input type="checkbox" name="exclude" value="yes" checked> Exclude tasks already attempted with this exact revision, model, effort and harness</label></fieldset>'
        content += '<p>Clear exclusion to explicitly rerun. Completed attempts are always preserved. Each run automatically receives independent evaluation and a model review.</p>'
        content += '<button name="operation" value="benchmark">Run and evaluate selected</button> <button name="operation" value="prepare">Prepare selected</button> <button name="operation" value="retire">Remove selected from active pool…</button>'
        body += form(pages, 'selection', content)
        body += '<h2>Prepare the pool</h2><p>Prepare all eligible unprepared tasks across repositories, including tasks outside the current filters. Each task runs cleanup, diagnostics, extraction, private validation when configured, independent review, and freezing. Rejected or failed work does not stop other tasks.</p>'
        body += form(pages, 'prepare-all', '<button>Prepare all eligible unprepared tasks</button>')
    else:
        body += content
    if not all_rows:
        body += '<p>Start with <code>mineval task add candidate.json</code>.</p>'
    return pages.page('Task pool', body)


def jobs(pages):
    if not pages.workflows:
        return pages.page('Work queue', '<p>Start <code>mineval observe</code> to enable historical workflows.</p>')
    body = '<p>Refresh for progress. Stages and IDs are durable. Retry preserves saved attempts and assessments. An interrupted benchmark is sealed as an infrastructure error before a new explicit rerun.</p><p>' + pages.link('Refresh queue', 'jobs') + '</p>'
    rows = []
    for job in pages.workflows.jobs():
        p = job['payload']; tid = job['task']
        config = p.get('configuration')
        detail = esc(job['detail'])
        if job['state'] in ('failed', 'interrupted'):
            new_attempt = pages.workflows.needs_new_attempt(job)
            route = 'new-attempt' if new_attempt else 'retry'
            label = 'Start new benchmark attempt' if new_attempt else ('Retry qualitative review' if job['stage'] in ('model review', 'review compaction') else 'Retry remaining stages')
            detail += form(pages, route, hidden('job', job['id']) + '<button>' + label + '</button>')
        refs = pages.task_link(tid)
        for field, route in (('run_id', 'runs'), ('evaluation_id', 'evaluations')):
            if (pages.store.task(tid) / route / (p[field] + '.json')).exists():
                refs += '<br>' + pages.link(field.removesuffix('_id'), 'tasks', tid, route, p[field])
                if route == 'evaluations':
                    assessment = pages.store._inspect_record(tid, 'evaluations', p[field])
                    refs += ' · ' + status(assessment.get('verdict', assessment['status']))
        rows.append((refs, esc(job['kind']) + ('<br>' + esc(config['model'] + ' · ' + config['effort']) if config else ''), status(job['state']), esc(job['stage']), detail))
    return pages.page('Work queue', body + table(['Task / results', 'Operation', 'State', 'Stage', 'Outcome / next step'], rows))


def percent(value):
    return 'Unavailable' if value is None else f'{value:.1%}'


def metrics(row):
    return [str(row[x]) for x in ('pass', 'fail', 'inconclusive', 'error')] + [percent(row['pass_rate']), str(row['evaluated']) + ' evaluated samples', str(row['decided']) + ' decided']


def models(pages, query):
    report = summaries(pages.store)
    body = '<p>Pass rate = pass / all evaluated samples; fail, inconclusive and error remain in the denominator. Samples pin exact task revisions and evaluation conditions. The first assessment of the earliest attempt per sample is used; reruns never select a best score. ' + pages.link('Compare explicit assessments', 'compare') + '.</p>'
    body += '<p class="notice">All-evidence figures describe each configuration’s own coverage and are not a ranking. Comparable rates below use only the exact common revision and evaluation-condition intersection across every listed configuration (' + str(report['common_samples']) + ' samples). No common samples means no comparable rate.</p>'
    rows = []
    for m in report['models']:
        label = m['model']['name'] + ' · ' + str(m['model'].get('reasoning_effort') or 'unspecified') + ' · ' + m['harness']['name']
        rows.append((esc(label), percent(m['overall']['pass_rate']), str(m['overall']['evaluated']), percent(m['category_average']) + ' (' + str(m['category_count']) + '/' + str(len(CATEGORIES)) + ' categories)', percent(m['comparable']['pass_rate']), pages.link('More details', 'models', model=m['model']['name'])))
    body += table(['Configuration', 'All-evidence pass rate', 'Evaluated samples', 'Category average', 'Comparable pass rate', 'Review'], rows)
    chosen = query.get('model', [None])[0]
    if chosen:
        body += '<h2>' + esc(chosen) + '</h2>'
        body += '<p>Category average weights each observed named category equally; unobserved categories are omitted and uncategorized tasks appear only in overall performance. Small samples are descriptive. Coverage is distinct evaluated tasks / prepared registered tasks in that category, including archived history.</p>'
        for m in report['models']:
            if m['model']['name'] != chosen:
                continue
            body += '<h3>Effort: ' + esc(m['model'].get('reasoning_effort') or 'unspecified') + ' · ' + esc(m['harness']['name']) + '</h3>'
            body += table(['Overall', 'Pass', 'Fail', 'Inconclusive', 'Error', 'Pass rate', 'Denominator', 'Decided count'], [('All evaluated samples', *metrics(m['overall']))])
            overall = m['overall']
            body += '<p>Task coverage: ' + str(overall['covered_tasks']) + '/' + str(overall['available']) + ' prepared tasks (' + percent(overall['coverage']) + '). Sample coverage: ' + str(overall['evaluated']) + '/' + str(overall['available_samples']) + ' observed revision/condition samples (' + percent(overall['sample_coverage']) + ').</p>'
            breakdown = []
            for category, r in m['categories'].items():
                breakdown.append((esc(category), *metrics(r), str(r['covered_tasks']) + '/' + str(r['available']) + ' tasks'))
            body += table(['Category', 'Pass', 'Fail', 'Inconclusive', 'Error', 'Pass rate', 'Denominator', 'Decided coverage', 'Task coverage'], breakdown)
            body += '<details><summary>Category performance on exact common conditions</summary>' + table(['Category', 'Pass', 'Fail', 'Inconclusive', 'Error', 'Pass rate', 'Denominator', 'Decided count'], [(esc(c), *metrics(r)) for c, r in m['comparable_categories'].items()]) + '</details>'
            body += '<details><summary>Exact assessment selections</summary>' + pretty(m['selections']) + '</details>'
        body += '<h2>Accumulated observations</h2><p>Qualitative observations supplement independent verdicts. Configuration and original evidence remain attached to each observation.</p>'
        for item in Memory(pages.store).history(chosen):
            body += '<ul>' + ''.join('<li>' + esc(x) + '</li>' for x in item['body']) + '</ul><details><summary>Sources and compaction provenance</summary>'
            for source in item['sources']:
                body += '<p>' + pages.link(source['task_id'] + ' / ' + source['evaluation_id'], 'tasks', source['task_id'], 'evaluations', source['evaluation_id']) + ' · ' + esc(source['model']) + '</p>'
            body += pretty({'observation_id': item['id'], 'parents': item['parents']}) + '</details>'
        body += '<details><summary>Recover original observations and summaries</summary>' + pretty(Memory(pages.store).history(chosen, archive=True)) + '</details>'
    if report['warnings']:
        body += '<p class="notice">' + esc('; '.join(report['warnings'])) + '</p>'
    return pages.page('Models and results', body)


def action(pages, route, fields):
    workflows = pages.workflows
    if route in ('retry', 'new-attempt'):
        workflows.retry(fields['job'][0], new_attempt=route == 'new-attempt')
        return pages.page('Retry queued', '<p>' + pages.link('View progress', 'jobs') + '</p>')
    if route == 'retire-confirm':
        if fields.get('confirm') != ['retire'] or not fields.get('reason', [''])[0].strip():
            raise ValueError('Confirm removal and supply a reason')
        outcomes = []
        for task in fields.get('task', []):
            try:
                pages.store.retire(task, fields['reason'][0])
                outcomes.append((pages.task_link(task), 'Removed from active pool; history preserved'))
            except (ValueError, OSError) as exc:
                outcomes.append((pages.task_link(task), esc(exc)))
        return pages.page('Tasks removed', table(['Task', 'Outcome'], outcomes))
    if route == 'prepare-all':
        selections = [{'task_id': t['task_id']} for t in tasks(pages.store) if not t['versions'] and t['state']['status'] not in ('retired', 'rejected', 'needs-review')]
        kind = 'prepare'
    elif route == 'selection':
        kind = fields.get('operation', [''])[0]
        selected = list(dict.fromkeys(fields.get('task', [])))
        if not selected:
            raise ValueError('Select at least one task')
        if kind == 'retire':
            content = '<p>Remove these tasks from the active pool? Retirement blocks future preparation and benchmarking, including suites containing these tasks. Revisions, runs and evaluations remain accessible. Retirement has no restore command.</p><ul>'
            content += ''.join('<li>' + esc(t) + hidden('task', t) + '</li>' for t in selected) + '</ul>'
            content += '<label>Reason <input name="reason" required maxlength="2000"></label><label><input type="checkbox" name="confirm" value="retire" required> Confirm removal from active pool</label><button>Remove tasks</button>'
            return pages.page('Confirm task removal', form(pages, 'retire-confirm', content))
        selections = [{'task_id': task, 'version': fields.get('version_' + task, [''])[0]} for task in selected]
    else:
        raise ValueError('Unknown operation')
    import json
    configs = [json.loads(v) for v in fields.get('configuration', [])]
    configs += [{'model': m.strip(), 'effort': fields.get('effort', ['medium'])[0], 'adapter': 'codex'}
                for m in fields.get('models', [''])[0].splitlines() if m.strip()]
    outcomes = workflows.enqueue(kind, selections, configs, exclude=fields.get('exclude') == ['yes'])
    pages.start_worker()
    return pages.page('Work submitted', '<p>' + pages.link('View progress and results', 'jobs') + '</p>' + table(['Task', 'Outcome', 'Details'], ((pages.task_link(r['task']), status(r['state']), esc(r.get('detail', ''))) for r in outcomes)))
