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


def eligible(task):
    return task['state']['status'] not in ('retired', 'rejected', 'needs-review')


def task_filters(rows, query):
    value = lambda key: query.get(key, [''])[0]
    result = rows
    for field in ('repository', 'category'):
        if value(field):
            result = [t for t in result if t[field] == value(field)]
    if value('status'):
        result = [t for t in result if t['state']['status'] == value('status')]
    if value('prepared'):
        result = [t for t in result if bool(t['versions']) == (value('prepared') == 'yes')]
    if value('search'):
        result = [t for t in result if value('search').casefold() in (t['task_summary'] + t['task_id'] + t['repository']).casefold()]
    key = value('sort')
    return sorted(result, key=lambda t: t.get(key, t['task_summary']).casefold() if key in ('repository', 'category', 'task_id') else t['task_summary'].casefold())


def filter_controls(rows, query):
    value = lambda key: query.get(key, [''])[0]
    content = '<label>Search<input name="search" placeholder="Search tasks…" value="' + esc(value('search')) + '"></label>'
    for field in ('repository', 'category'):
        content += select(field, [('', 'All ' + field + 's')] + [(x, x) for x in sorted({t[field] for t in rows})], value(field))
    content += select('status', [('', 'All states')] + [(x, x) for x in sorted({t['state']['status'] for t in rows})], value('status'))
    content += select('prepared', [('', 'All tasks'), ('yes', 'Prepared'), ('no', 'Unprepared')], value('prepared'), 'Preparation')
    content += select('sort', [('title', 'Title'), ('repository', 'Repository'), ('category', 'Category'), ('task_id', 'Identifier')], value('sort'), 'Sort by')
    return '<div class="toolbar">' + content + '<button>Apply filters</button></div>'


def task_choices(pages, rows, selected=(), revision_query=None):
    revision_query = revision_query or {}
    result = []
    for t in rows:
        tid = t['task_id']
        check = '<input aria-label="Select ' + esc(tid) + '" type="checkbox" name="task" value="' + esc(tid) + '"' + (' checked' if tid in selected else '') + '>'
        revisions = select('version_' + tid, [(v, v) for v in t['versions']], revision_query.get('version_' + tid, [t['state'].get('version', '')])[0], 'Frozen revision') if t['versions'] else '<span class="muted">Unprepared</span>'
        result.append((check, pages.link(t['task_summary'], 'tasks', tid) + '<small>' + esc(tid) + '</small>', esc(t['repository']), esc(t['category']), status(t['state']['status']), revisions))
    return '<div class="task-pool">' + table(['Select', 'Task', 'Repository', 'Category', 'State', 'Preparation'], result) + '</div>'


def pool(pages, query):
    all_rows = tasks(pages.store)
    rows = task_filters(all_rows, query)
    prepared = sum(bool(t['versions']) for t in all_rows)
    body = '<div class="heading"><p>Manage historical tasks across repositories. ' + str(len(all_rows)) + ' tasks · ' + str(prepared) + ' prepared</p>'
    if pages.workflows:
        body += '<a class="button" href="' + esc(pages.url('prepare', all='yes')) + '">Prepare all</a>'
    body += '</div><form method="get">' + filter_controls(all_rows, query) + '</form>'
    if pages.workflows:
        selected = [t['task_id'] for t in rows] if query.get('select') == ['visible'] else query.get('task', [])
        # Selection is an explicit form snapshot. Filters never carry hidden selections.
        body += '<p class="muted">Selection applies to the visible table. Applying filters clears selection. ' + pages.link('Select all visible', 'tasks', **(query | {'select': 'visible'})) + ' · ' + pages.link('Clear selection', 'tasks', **{k: v for k, v in query.items() if k not in ('select', 'task')}) + '</p>'
        body += '<form method="get" action="' + esc(pages.url('benchmarks', 'new')) + '"><div class="toolbar selection-bar"><strong>Selected tasks</strong><button formaction="' + esc(pages.url('prepare')) + '">Prepare selected</button><button>Benchmark selected</button>'
        body += '<button class="secondary" formaction="' + esc(pages.url('remove')) + '">Remove selected…</button></div>' + task_choices(pages, rows, selected, query) + '</form>'
    else:
        body += table(['Task', 'Repository', 'Category', 'State'], [(pages.link(t['task_summary'], 'tasks', t['task_id']), esc(t['repository']), esc(t['category']), status(t['state']['status'])) for t in rows])
    if not all_rows:
        body += '<p class="notice">No historical tasks yet. Add a selector handoff with <code>mineval task add candidate.json</code>.</p>'
    elif not rows:
        body += '<p>No tasks match. ' + pages.link('Clear filters', 'tasks') + '</p>'
    return pages.page('Tasks', body)


def agent_summary(pages, preparation=False):
    if not pages.workflows:
        return ''
    w = pages.workflows
    current = w.model_settings()
    role = 'preparation' if preparation else 'evaluation'
    config = current[role]
    review_model = w.settings.get('review_model', config['model'])
    if preparation:
        review_effort = (w.settings.get('review_effort') or 'runtime default') if w.settings.get('review_model') else config['effort']
    else:
        review_effort = w.settings.get('review_effort', config['effort'])

    text = ('Cleanup & extraction: ' + esc(config['model'] + ' · ' + config['effort'])) if preparation else 'Fixed functional grading'
    text += '. ' + ('Independent preparation review: ' if preparation else 'Architecture review: ')
    if preparation or w.settings.get('architecture_review', False):
        text += esc(str(review_model) + ' · ' + str(review_effort))
        if w.settings.get('review_model') or (not preparation and w.settings.get('review_effort')):
            text += ' (host-controlled review override)'
    else:
        text += 'disabled'
    return '<p class="notice">' + text + '. ' + pages.link('Agents & models settings', 'settings') + '</p>'


def execution_host(pages):
    choices = [('local', 'Local')]
    if pages.workflows.settings.get('vps', {}).get('host'):
        choices.append(('vps', 'VPS'))
    return select('execution_host', choices, 'local', 'Execution host')


def prepare(pages, query):
    if not pages.workflows:
        return pages.page('Prepare tasks', '<p>Preparation is available in the operational observe workspace.</p>')
    pool = tasks(pages.store)
    selected = set(query.get('task', []))
    chosen = pool if query.get('all') == ['yes'] else [t for t in pool if t['task_id'] in selected]
    ready = [t for t in chosen if eligible(t) and not t['versions']]
    skipped = [t for t in chosen if t not in ready]
    content = '<p>' + str(len(ready)) + ' tasks will be prepared; ' + str(len(skipped)) + ' skipped (already prepared or unavailable).</p>' + agent_summary(pages, True)
    content += '<p>Cleanup → diagnostics → private verification extraction → validation → independent review → freeze. Failures are isolated per task.</p>'
    content += table(['Task', 'Outcome'], [(pages.task_link(t['task_id']), 'Prepare' if t in ready else 'Skip · ' + esc(t['state']['status'])) for t in chosen])
    content += ''.join(hidden('task', t['task_id']) for t in ready) + hidden('operation', 'prepare')
    content += execution_host(pages)
    content += '<div class="actions"><button' + ('' if ready else ' disabled') + '>Prepare ' + str(len(ready)) + ' tasks</button>' + pages.link('Cancel', 'tasks') + '</div>'
    return pages.page('Prepare tasks', form(pages, 'selection', content))


def benchmarks(pages, query, new=False):
    if not new or not pages.workflows:
        body = '<div class="heading"><p>Benchmark frozen tasks and inspect their automatically evaluated results.</p>'
        if pages.workflows:
            body += '<a class="button" href="' + esc(pages.url('benchmarks', 'new')) + '">Run benchmark</a>'
        body += '</div><p>' + pages.link('Saved suites', 'suites') + ' · ' + pages.link('Compare explicit assessments', 'compare') + ' · ' + pages.link('Progress and results', 'jobs') + '</p>'
        return pages.page('Benchmarks', body + comparison_table(pages, summaries(pages.store)))
    step = query.get('step', ['tasks'])[0]
    pool = tasks(pages.store)
    available = [t for t in pool if t['versions'] and eligible(t)]
    selected = set(query.get('task', []))
    chosen = [t for t in available if t['task_id'] in selected]
    excluded = len(selected - {t['task_id'] for t in chosen})
    body = '<p class="step">Choose tasks → Choose benchmark models → Review and run</p>'
    if step == 'tasks':
        rows = task_filters(available, query)
        body += '<p>' + str(len(available)) + ' eligible prepared tasks. Unprepared, rejected, review-required and retired tasks are unavailable. ' + str(excluded) + ' incoming selections excluded.</p>'
        selected = {t['task_id'] for t in rows} | selected if query.get('select') == ['visible'] else selected
        visible_ids = {t['task_id'] for t in rows}
        retained = ''.join(hidden('task', t['task_id']) + hidden('version_' + t['task_id'], query.get('version_' + t['task_id'], [t['state'].get('version') or t['versions'][0]])[0]) for t in chosen if t['task_id'] not in visible_ids)
        body += '<form method="get">' + filter_controls(available, query).replace('<button>Apply filters</button>', '<button name="step" value="tasks">Apply filters</button>') + retained
        body += '<div class="actions"><button name="step" value="models">Choose benchmark models</button><button class="secondary" name="select" value="visible">Select all visible</button>' + pages.link('Cancel', 'benchmarks') + '</div>' + task_choices(pages, rows, selected, query) + '</form>'
        return pages.page('Run benchmark · Choose tasks', body)
    if not chosen:
        return pages.page('Choose prepared tasks', '<p>No eligible tasks selected. ' + pages.link('Choose tasks', 'benchmarks', 'new') + '</p>')
    carry = ''.join(hidden('task', t['task_id']) + hidden('version_' + t['task_id'], query.get('version_' + t['task_id'], [t['state'].get('version') or t['versions'][0]])[0]) for t in chosen)
    if step == 'models':
        catalog = pages.workflows.catalog.get()
        from .workflows import configuration
        presets = pages.workflows.settings.get('models', [])
        choices = [(m['model'], m['name'], m['efforts'] or list(EFFORTS), m['default_effort'], 'codex') for m in catalog['models']]
        choices += [(c['model'], c['model'] + ' · host preset', list(EFFORTS), c['effort'], c['adapter']) for c in map(configuration, presets)]
        # A row per model/effort permits several configurations of the same model.
        search = query.get('model_search', [''])[0].casefold()
        picker = []
        previous = set(query.get('configuration', []))
        for model, name, efforts, default, adapter in choices:
            if search and search not in (model + name).casefold():
                continue
            effort_choices = ''
            for effort in efforts:
                c = json.dumps({'model': model, 'effort': effort, 'adapter': adapter})
                effort_choices += '<label><input type="checkbox" name="configuration" value="' + esc(c) + '"' + (' checked' if c in previous else '') + '> ' + esc(effort) + (' <small>default</small>' if effort == default else '') + '</label>'
            picker.append((esc(name) + '<small>' + esc(model) + '</small>', '<div class="effort-choices">' + effort_choices + '</div>', esc(adapter)))
        body += '<h2>Models being benchmarked</h2><p>' + str(len(chosen)) + ' prepared tasks selected. Choose one or more model and reasoning-effort configurations.</p>'
        if catalog['error']:
            body += '<p class="notice">' + esc(catalog['error']) + '</p>'
        content = carry + hidden('model_step', 'yes') + '<div class="toolbar"><label>Search models <input name="model_search" value="' + esc(query.get('model_search', [''])[0]) + '"></label><button name="step" value="models">Search models</button></div>'
        # Preserve configurations outside the search results.
        visible = {json.dumps({'model': model, 'effort': effort, 'adapter': adapter}) for model, name, efforts, default, adapter in choices if not search or search in (model + name).casefold() for effort in efforts}
        content += ''.join(hidden('configuration', c) for c in previous - visible)
        content += '<div class="model-picker">' + table(['Model', 'Reasoning efforts · choose configurations', 'Harness'], picker) + '</div>'
        content += '<details><summary>Explicit model identifiers</summary><label>One identifier per line<textarea name="models" rows="3">' + esc(query.get('models', [''])[0]) + '</textarea></label>' + select('effort', [(x, x) for x in EFFORTS], query.get('effort', ['medium'])[0], 'Reasoning effort for explicit models') + '</details>'
        content += '<label>Attempts per task and model <input type="number" name="attempts" min="1" max="100" value="' + esc(query.get('attempts', ['1'])[0]) + '" required></label>'
        content += '<label><input type="checkbox" name="exclude" value="yes"' + (' checked' if 'model_step' not in query or query.get('exclude') == ['yes'] else '') + '> Skip already benchmarked tasks</label><p class="muted">Skips attempts with the same complete frozen revision, model, effort and harness, including unsuccessful attempts.</p><button name="step" value="review">Review benchmark</button>'
        body += '<form method="get">' + content + '</form>'
        return pages.page('Run benchmark · Choose models', body)
    configs = [json.loads(v) for v in query.get('configuration', [])]
    configs += [{'model': m.strip(), 'effort': query.get('effort', ['medium'])[0], 'adapter': 'codex'} for m in query.get('models', [''])[0].splitlines() if m.strip()]
    from .workflows import configuration, matches
    configs = list({json.dumps(configuration(c), sort_keys=True): configuration(c) for c in configs}.values())
    from .benchmark import attempt_count
    attempts = attempt_count(int(query.get('attempts', ['1'])[0]))
    outcomes = []
    runnable = 0
    for t in chosen:
        version = query.get('version_' + t['task_id'], [t['state'].get('version') or t['versions'][0]])[0]
        try:
            revision = pages.store._inspect_revision(t['task_id'], version)
            pages.workflows.checked_config(pages.workflows.eval_config(t['task_id'], version))
            prior = [pages.store.inspect_run(t['task_id'], p.stem) for p in (pages.store.task(t['task_id']) / 'runs').glob('*.json')]
            for c in configs:
                skip = query.get('exclude') == ['yes'] and any(matches(r, revision, c) for r in prior)
                if not skip:
                    runnable += attempts
                outcomes.append((pages.task_link(t['task_id']), esc(c['model'] + ' · ' + c['effort']), 'Skip · already attempted' if skip else 'Run and automatically evaluate'))
        except (ValueError, OSError) as exc:
            outcomes.append((pages.task_link(t['task_id']), 'Unavailable', esc(exc)))
    body += '<p>' + str(len(chosen)) + ' tasks × ' + str(len(configs)) + ' configurations. Queue submission rechecks eligibility and active work.</p>' + table(['Task', 'Configuration', 'Planned outcome'], outcomes)
    body += '<p>Saved private checks run automatically after each attempt. Deterministic checks do not require an AI model. Optional architecture review follows the effective settings below; functional results are available as soon as grading finishes.</p>' + agent_summary(pages)
    body += '<p>' + str(runnable) + (' attempt' if runnable == 1 else ' attempts') + ' ready to queue. Unavailable tasks and prior attempts above are excluded or reported individually; other tasks continue.</p>'
    content = carry + hidden('attempts', attempts) + hidden('operation', 'benchmark') + ''.join(hidden('configuration', json.dumps(c)) for c in configs)
    if query.get('exclude') == ['yes']:
        content += hidden('exclude', 'yes')
    content += execution_host(pages)
    content += '<div class="actions"><button' + ('' if runnable else ' disabled') + '>Run benchmark</button>' + pages.link('Start over', 'benchmarks', 'new', task=[t['task_id'] for t in chosen]) + '</div>'
    return pages.page('Run benchmark · Review and run', body + form(pages, 'selection', content))


def _budget_label(conditions):
    limits = conditions.get('limits')
    if not limits:
        return 'Unavailable'
    coding = f"{limits['timeout_seconds']:g}s · {limits['cpus']:g} CPU · {limits['memory_mb']} MiB"
    grading = (conditions.get('grading') or {}).get('limits')
    if grading:
        coding += f" / grading {grading['timeout_seconds']:g}s · {grading['cpus']:g} CPU · {grading['memory_mb']} MiB"
    return coding


def comparison_table(pages, report):
    rows = []
    for m in report['models']:
        overall = m['overall']
        comparable = m['comparable']
        rate = percent(comparable['pass_rate'])
        bar = ''
        if comparable['pass_rate'] is not None:
            bar = f'<meter min="0" max="1" value="{comparable["pass_rate"]}">{rate}</meter>'
        rows.append((
            pages.link(m['model']['name'], 'models', model=m['model']['name']),
            esc(m['model'].get('reasoning_effort') or 'unspecified'), esc(m['harness']['name']),
            f'<span class="rate">{rate}{bar}</span>', str(comparable['attempt_count']),
            percent(overall['pass_rate']), f'{overall["pass"]} / {overall["decided"]}',
            f'{overall["covered_tasks"]} / {overall["available"]} tasks',
            f'{m["category_count"]} / {len(CATEGORIES)}',
            str(overall['attempt_count']), str(overall['infrastructure_errors']),
            esc(_budget_label(m['conditions'])),
        ))
    return table(['Model', 'Reasoning', 'Harness', 'Mean matched-task pass rate', 'Matched attempts',
                  'All-evidence pass rate', 'Passed / decided', 'Task coverage', 'Categories',
                  'Attempts', 'Infrastructure errors', 'Run budget'], rows)


def overview(pages):
    pool = tasks(pages.store); report = summaries(pages.store)
    jobs_list = pages.workflows.jobs() if pages.workflows else []
    prepared = sum(bool(t['versions']) for t in pool)
    body = '<div class="heading"><p>Historical coding benchmarks across real repositories and engineering tasks.</p>'
    if pages.workflows:
        body += '<a class="button" href="' + esc(pages.url('benchmarks', 'new')) + '">Run benchmark</a>'
    body += '</div><div class="stats">'
    values = [('Prepared tasks', str(prepared), str(len(pool)) + ' registered tasks'), ('Models evaluated', str(len({m['model']['name'] for m in report['models']})), str(len(report['models'])) + ' configurations'), ('Active operations', str(sum(j['state'] in ('queued', 'running') for j in jobs_list)) if pages.workflows else 'Unavailable', 'Queued and running' if pages.workflows else 'Open operational workspace'), ('Needs attention', str(sum(j['state'] in ('failed', 'interrupted') for j in jobs_list)) if pages.workflows else 'Unavailable', 'Failed and interrupted' if pages.workflows else 'Open operational workspace')]
    body += ''.join('<div class="stat"><span>' + name + '</span><strong>' + value + '</strong><small>' + sub + '</small></div>' for name, value, sub in values) + '</div>'
    body += '<h2>Model comparison</h2><p class="muted">All-evidence rates describe each configuration’s own task set; they are not rankings. Comparable rates use ' + str(report['common_samples']) + ' matching revision and evaluation-condition samples shared by every configuration.</p>' + comparison_table(pages, report)
    if not report['models']:
        body += '<p class="notice">No evaluated configurations yet. Prepare tasks, then run your first benchmark. ' + pages.link('Task pool', 'tasks') + '</p>'
    heat = []
    for m in report['models']:
        cells = [pages.link(m['model']['name'] + ' · ' + str(m['model'].get('reasoning_effort') or 'unspecified'), 'models', model=m['model']['name'])]
        for category in CATEGORIES:
            r = m['categories'][category]; rate = r['pass_rate']
            tone = '' if rate is None else 'good' if rate >= .8 else 'mid' if rate >= .6 else 'low'
            cells.append('<span class="heat ' + tone + '">' + percent(rate) + '<small>' + str(r['pass']) + '/' + str(r['evaluated']) + ' samples · ' + str(r['covered_tasks']) + '/' + str(r['available']) + ' tasks</small></span>')
        heat.append(cells)
    body += '<h2>Category performance matrix</h2><p class="muted">Fixed engineering taxonomy. Unobserved categories are unavailable; denominators include all verdicts.</p>' + table(['Configuration', *CATEGORIES], heat)
    body += '<h2>Recent activity</h2>' + table(['Task', 'Operation', 'State', 'Stage'], [(pages.task_link(j['task']), esc(j['kind']), status(j['state']), esc(j['stage'])) for j in jobs_list[:8]]) + '<p>' + pages.link('View all activity', 'jobs') + '</p>'
    if report['warnings']:
        body += '<p class="notice">' + esc('; '.join(report['warnings'])) + '</p>'
    return pages.page('Benchmark overview', body)


def jobs(pages, query=None):
    pages.active = 'jobs'
    query = query or {}
    if not pages.workflows:
        return pages.page('Work queue', '<p>Start <code>mineval observe</code> to enable historical workflows.</p>')
    body = '<p>Preparation stages: cleanup → diagnostics → extraction → validation → independent review → freeze. Benchmark stages: attempt → private evaluation → factual observation and optional architecture review.</p><p>Refresh for progress. Stages and IDs are durable. Retry preserves saved attempts and assessments. An interrupted benchmark is sealed as an infrastructure error before a new explicit rerun.</p><p>' + pages.link('Refresh queue', 'jobs') + '</p>'
    state_filter = query.get('state', [''])[0]
    body += '<div class="tabs">' + ' '.join(pages.link(label, 'jobs', state=value) for value, label in [('', 'All'), ('queued', 'Queued'), ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed'), ('interrupted', 'Interrupted')]) + '</div>'
    rows = []
    for job in pages.workflows.jobs():
        if state_filter and job['state'] != state_filter:
            continue
        p = job['payload']; tid = job['task']
        config = p.get('configuration')
        detail = esc(job['detail'])
        if job['state'] in ('failed', 'interrupted'):
            new_attempt = pages.workflows.needs_new_attempt(job)
            route = 'new-attempt' if new_attempt else 'retry'
            label = 'Start new benchmark attempt' if new_attempt else 'Retry remaining stages'
            detail += form(pages, route, hidden('job', job['id']) + '<button>' + label + '</button>')
        if job['state'] == 'running' and p.get('execution_host') == 'vps':
            detail += form(pages, 'cancel', hidden('job', job['id']) + '<button>Cancel VPS job</button>')
        detail += '<details><summary>Queued agent configuration</summary>' + pretty(p.get('model_settings', {'note': 'Legacy job resolves defaults when executed'})) + '</details>'
        refs = pages.task_link(tid)
        for field, route in (('run_id', 'runs'), ('evaluation_id', 'evaluations')):
            if (pages.store.task(tid) / route / (p[field] + '.json')).exists():
                refs += '<br>' + pages.link(field.removesuffix('_id'), 'tasks', tid, route, p[field])
                if route == 'evaluations':
                    assessment = pages.store._inspect_record(tid, 'evaluations', p[field])
                    refs += ' · ' + status(assessment.get('verdict', assessment['status']))
        rows.append((refs, esc(job['kind']) + '<br>' + esc(p.get('execution_host', 'local').upper()) + ('<br>' + esc(config['model'] + ' · ' + config['effort']) if config else ''), status(job['state']), esc(job['stage']), detail))
    return pages.page('Activity', body + table(['Task / results', 'Operation', 'State', 'Stage', 'Outcome / next step'], rows))


def percent(value):
    return 'Unavailable' if value is None else f'{value:.1%}'


def metrics(row):
    return [str(row[x]) for x in ('pass', 'fail', 'inconclusive', 'error')] + [percent(row['pass_rate']), str(row['evaluated']) + ' evaluated attempts', str(row['decided']) + ' decided']


def models(pages, query):
    report = summaries(pages.store)
    chosen = query.get('model', [None])[0]
    body = '<p>Pass rates use pass and fail verdicts. Exact task revisions and evaluation conditions define matches. Each matched task with a decided attempt contributes equally to the comparable rate.</p>'
    body += '<p class="notice">All-evidence figures describe each configuration’s own coverage. Comparable rates use the exact common revision and evaluation-condition intersection (' + str(report['common_samples']) + ' matches).</p>'
    if not chosen:
        body += comparison_table(pages, report)
        body += '<p>' + pages.link('Compare explicit assessments', 'compare') + '</p>'
    else:
        body = '<div class="profile"><p>' + pages.link('All models', 'models') + '</p><p>Performance and observations across reasoning efforts. Independent verdicts remain authoritative.</p>'
        body += '<div class="tabs"><a href="#performance">Overview</a><a href="#categories">Category breakdown</a><a href="#observations">Review notes</a><a href="#runs">Historical runs</a></div><h2 id="performance">Evaluated performance</h2>'
    if chosen:
        body += '<h2>' + esc(chosen) + '</h2>'
        body += '<p>Category average weights each observed named category equally; unobserved categories are omitted and uncategorized tasks appear only in overall performance. Small samples are descriptive. Coverage is distinct evaluated tasks / prepared registered tasks in that category, including archived history.</p>'
        for m in report['models']:
            if m['model']['name'] != chosen:
                continue
            body += '<h3>Effort: ' + esc(m['model'].get('reasoning_effort') or 'unspecified') + ' · ' + esc(m['harness']['name']) + '</h3>'
            body += table(['Overall', 'Pass', 'Fail', 'Inconclusive', 'Error', 'Pass rate', 'Denominator', 'Decided count'], [('All evaluated attempts', *metrics(m['overall']))])
            overall = m['overall']
            body += '<p>Task coverage: ' + str(overall['covered_tasks']) + '/' + str(overall['available']) + ' prepared tasks (' + percent(overall['coverage']) + '). Sample coverage: ' + str(overall['evaluated']) + '/' + str(overall['available_samples']) + ' observed revision/condition samples (' + percent(overall['sample_coverage']) + ').</p>'
            breakdown = []
            for category, r in m['categories'].items():
                breakdown.append((esc(category), *metrics(r), str(r['covered_tasks']) + '/' + str(r['available']) + ' tasks'))
            body += '<h2 id="categories">Category performance</h2>' + table(['Category', 'Pass', 'Fail', 'Inconclusive', 'Error', 'Pass rate', 'Denominator', 'Decided coverage', 'Task coverage'], breakdown)
            body += '<details><summary>Category performance on exact common conditions</summary>' + table(['Category', 'Pass', 'Fail', 'Inconclusive', 'Error', 'Mean matched-task pass rate', 'Attempts', 'Decided attempts'], [(esc(c), *metrics(r)) for c, r in m['comparable_categories'].items()]) + '</details>'
            body += '<details><summary>Exact assessment selections</summary>' + pretty(m['selections']) + '</details>'
        body += '<h2 id="observations">Accumulated observations</h2><p>Qualitative observations supplement independent verdicts. Configuration and original evidence remain attached to each observation.</p>'
        for item in Memory(pages.store).history(chosen):
            body += '<ul>' + ''.join('<li>' + esc(x) + '</li>' for x in item['body']) + '</ul><details><summary>Sources and compaction provenance</summary>'
            for source in item['sources']:
                body += '<p>' + pages.link(source['task_id'] + ' / ' + source['evaluation_id'], 'tasks', source['task_id'], 'evaluations', source['evaluation_id']) + ' · ' + esc(source['model']) + '</p>'
            body += pretty({'observation_id': item['id'], 'parents': item['parents']}) + '</details>'
        body += '<details><summary>More details · Recover original observations and summaries</summary>' + pretty(Memory(pages.store).history(chosen, archive=True)) + '</details>'
    if chosen:
        run_rows = []
        for t in tasks(pages.store):
            for path in sorted((pages.store.task(t['task_id']) / 'runs').glob('*.json')):
                r = pages.store.inspect_run(t['task_id'], path.stem)
                if r['model']['name'] == chosen:
                    run_rows.append((pages.link(r['run_id'], 'tasks', t['task_id'], 'runs', r['run_id']), pages.task_link(t['task_id']), esc(r['model'].get('reasoning_effort') or 'unspecified'), status(r['status']), pages.revision_link(r['prepared_revision'])))
        body += '<h2 id="runs">Historical runs</h2>' + table(['Run', 'Task', 'Reasoning', 'State', 'Revision'], run_rows) + '</div>'
    if report['warnings']:
        body += '<p class="notice">' + esc('; '.join(report['warnings'])) + '</p>'
    return pages.page(chosen or 'Models', body)


def settings(pages, *, refreshed=False, saved=False):
    if not pages.workflows:
        return pages.page('Model settings', '<p>Start <code>mineval observe</code> to configure workflow models.</p>')
    workflow = pages.workflows
    catalog = workflow.catalog.get(refresh=refreshed)
    current = workflow.model_settings()
    body = '<p>Agents & models. Configure persistent defaults for preparation and optional AI assistance. Benchmark models are selected separately when starting a run. Deterministic private checks do not require an AI model. Settings apply to newly queued work; queued jobs and retries keep their original choices.</p>'
    if saved:
        body += '<p class="notice" role="status">Model settings saved.</p>'
    if catalog['error']:
        body += '<p class="notice">' + esc(catalog['error']) + '</p>'
    elif not catalog['models']:
        body += '<p class="notice">Codex returned no visible models. Enter an explicit identifier or refresh after signing in.</p>'
    body += form(pages, 'refresh-models', '<button>Refresh Codex models</button>')
    options = ''.join('<option value="' + esc(m['model']) + '">' + esc(m['name']) + '</option>' for m in catalog['models'])
    content = '<datalist id="codex-models">' + options + '</datalist>'
    for role, title, explanation in (
            ('preparation', 'Preparation agents · Prep / extract', 'Cleanup and private verification extraction. Independent preparation review uses this model unless the host review override below applies.'),
            ('evaluation', 'Architecture review agent', 'Architecture review uses this default unless the host review override applies. Fixed private checks run without an AI model.')):
        config = current[role]
        content += '<fieldset><legend>' + title + '</legend><p>' + explanation + '</p>'
        content += '<label>Model <input name="' + role + '_model" list="codex-models" value="' + esc(config['model']) + '" required maxlength="200" spellcheck="false"></label>'
        content += select(role + '_effort', [(x, x) for x in EFFORTS], config['effort'], 'Reasoning effort')
        content += '</fieldset>'
    content += '<p>Choose a discovered model or type an explicit identifier. Supported efforts are listed below; availability also depends on the Codex runtime in your configured image.</p><button>Save model settings</button>'
    body += form(pages, 'model-settings', content)
    body += '<h2>Effective role configuration</h2>' + agent_summary(pages, True) + agent_summary(pages)
    body += '<h2>Available Codex models</h2><div class="model-catalog">' + table(['Model', 'Identifier', 'Reasoning efforts', 'Default effort'], (
        (esc(m['name']), '<code>' + esc(m['model']) + '</code>', esc(', '.join(m['efforts']) or 'Not reported'), esc(m['default_effort']))
        for m in catalog['models'])) + '</div>'
    body += '<p>Host-controlled environment: preparation runtime ' + ('configured' if workflow.settings.get('preparation_image') else 'not configured') + '; fixed functional grading enabled; architecture review ' + ('enabled' if workflow.settings.get('architecture_review', False) else 'disabled') + '. Runtime images, private commands and credentials are managed by the trusted launcher.</p>'
    return pages.page('Settings · Agents & models', body)


def action(pages, route, fields):
    workflows = pages.workflows
    if route == 'refresh-models':
        return settings(pages, refreshed=True)
    if route == 'model-settings':
        workflows.save_model_settings({role: {'model': fields.get(role + '_model', [''])[0].strip(),
            'effort': fields.get(role + '_effort', ['medium'])[0], 'adapter': 'codex'}
            for role in ('preparation', 'evaluation')})
        return settings(pages, saved=True)
    if route == 'cancel':
        workflows.cancel(fields['job'][0])
        return jobs(pages)
    if route in ('retry', 'new-attempt'):
        workflows.retry(fields['job'][0], new_attempt=route == 'new-attempt')
        return jobs(pages)
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
    outcomes = workflows.enqueue(kind, selections, configs, exclude=fields.get('exclude') == ['yes'],
                                 execution_host=fields.get('execution_host', ['local'])[0], attempts=int(fields.get('attempts', ['1'])[0]))
    pages.start_worker()
    submission = table(['Task', 'Submission outcome', 'Details'], ((pages.task_link(r['task']), status(r['state']), esc(r.get('detail', ''))) for r in outcomes))
    progress = jobs(pages)
    return progress.replace('<h1>Activity</h1>', '<h1>Activity</h1><h2>Submission results</h2>' + submission, 1)
