"""Normalize portable selector records without discovering or executing anything."""
import hashlib
import json
from pathlib import Path

from mineval.core.artifacts import read_json, relative


def load_candidate(path):
    """Source paths in a handoff are relative to that file, not the caller's cwd."""
    path = Path(path).resolve(strict=True)
    candidate = read_json(path)
    if isinstance(candidate, dict):
        for field in ('base_source', 'future_source'):
            source = candidate.get(field)
            if isinstance(source, dict) and isinstance(source.get('path'), str):
                source['path'] = str((path.parent / source['path']).resolve())
        if 'task_brief' in candidate:
            if 'task_instructions' in candidate:
                raise ValueError('Supply one task brief')
            candidate['task_instructions'] = read_brief(path.parent / candidate.pop('task_brief'))
    return candidate


def read_brief(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ValueError(f'Task brief must be a regular document of at most 256 KiB: {path}')
    value = path.read_text()
    if not value.strip():
        raise ValueError('Task brief must be nonempty')
    return value


def normalize(candidate):
    from mineval.core.store import commit, identifier

    if not isinstance(candidate, dict):
        raise ValueError('Candidate must be a JSON object')
    candidate = dict(candidate)
    from mineval.historical.categories import CATEGORIES, LEGACY_LABELS
    schema = candidate.get('schema_version', 1)
    if type(schema) is not int or schema not in (1, 2):
        raise ValueError('Unsupported discovery schema_version; expected 1 or 2')
    if schema == 2 and (not isinstance(candidate.get('category'), str) or candidate['category'] not in CATEGORIES):
        raise ValueError('New discovery records require a primary category: ' + ', '.join(CATEGORIES))
    if 'category' in candidate and (not isinstance(candidate['category'], str) or
            candidate['category'] not in CATEGORIES.keys() | LEGACY_LABELS):
        raise ValueError('category must be one of: ' + ', '.join(CATEGORIES))
    for field in ('repository', 'task_summary'):
        if not isinstance(candidate.get(field), str) or not candidate[field].strip():
            raise ValueError(f'{field} must be a nonempty string')
    if 'task_instructions' in candidate:
        brief = candidate['task_instructions']
        if not isinstance(brief, str) or not brief.strip() or len(brief.encode()) > 256 * 1024:
            raise ValueError('task_instructions must be nonempty text of at most 256 KiB')
    for field in ('base_commit', 'head_commit'):
        if candidate.get(field) is not None:
            commit(candidate[field])
    for field in ('verification_feasibility', 'why_high_signal', 'source_attestation'):
        if field in candidate and (not isinstance(candidate[field], str) or not candidate[field].strip()):
            raise ValueError(f'{field} must be a nonempty string; omit unknown facts')
    for field in ('future_commits', 'related_commits', 'related_prs', 'future_evidence',
                  'uncertainty', 'selection_reasons'):
        if field in candidate and not isinstance(candidate[field], list):
            raise ValueError(f'{field} must be a list')
    for field in ('future_commits', 'related_commits'):
        for sha in candidate.get(field, []):
            commit(sha)
    for field in ('related_prs', 'uncertainty', 'selection_reasons'):
        if any(not isinstance(x, str) or not x.strip() for x in candidate.get(field, [])):
            raise ValueError(f'{field} entries must be nonempty strings')
    for field in ('base_source', 'future_source'):
        if candidate.get(field) is not None:
            source = candidate[field]
            if field == 'base_source' and isinstance(source, dict) and source.get('type') != 'git':
                raise ValueError('Intake requires Git history via base_source.type=git')
            allowed = ('git',) if field == 'base_source' else ('tree', 'archive')
            if (not isinstance(source, dict) or source.get('type') not in allowed
                    or not isinstance(source.get('path'), str) or not source['path'].strip()):
                raise ValueError(f'{field} requires a supported type and local path')
    evidence = candidate.get('future_evidence', [])
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError('future_evidence entries must be objects')
        commit(item.get('commit'))
        if not isinstance(item.get('proves'), str) or not item['proves'].strip():
            raise ValueError('future_evidence requires a nonempty proves description')
        if 'path' in item:
            relative(item['path'])
        elif not isinstance(item.get('reproduction'), str) or not item['reproduction'].strip():
            raise ValueError('future_evidence requires path or reproducible verification instructions')
    execution = candidate.get('execution', {})
    if not isinstance(execution, dict):
        raise ValueError('execution must be an object')
    for field in ('likely_commands', 'external_requirements', 'cleanup_risks'):
        if field in execution and (not isinstance(execution[field], list)
                                  or any(not isinstance(x, str) for x in execution[field])):
            raise ValueError(f'execution.{field} must be a list of strings')
    selection = candidate.get('selection_status', 'accept')
    if selection not in ('accept', 'reject', 'defer'):
        raise ValueError('selection_status must be accept, reject or defer')
    futures = list(dict.fromkeys(candidate.get('future_commits', [])
                                + candidate.get('related_commits', [])
                                + ([candidate['head_commit']] if candidate.get('head_commit') else [])
                                + [item['commit'] for item in evidence]))
    if len(futures) > 50:
        raise ValueError('At most 50 explicit future commits per candidate')
    if candidate.get('base_commit') in futures:
        raise ValueError('Future evidence must be after the base')
    missing = [field for field in ('base_commit', 'head_commit', 'base_source') if not candidate.get(field)]
    # Legacy records with explicit future_commits remain importable.
    blocked = not candidate.get('base_commit') or not candidate.get('base_source') or not futures
    if not evidence:
        missing.append('future_evidence')
    if not candidate.get('verification_feasibility'):
        missing.append('verification_feasibility')
    if 'cleanup_risks' not in execution:
        missing.append('execution.cleanup_risks')
    if 'uncertainty' not in candidate:
        missing.append('uncertainty')
    if not candidate.get('task_id'):
        identity = [candidate['repository'], candidate.get('base_commit'), candidate.get('head_commit') or sorted(futures)]
        if blocked:
            identity.append(candidate['task_summary'])
        candidate['task_id'] = 'task-' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
    identifier(candidate['task_id'])
    candidate['future_commits'] = futures
    candidate['intake'] = {'schema_version': 1, 'missing_fields': missing,
                           'status': 'incomplete' if missing or candidate.get('uncertainty') or selection == 'defer' else 'complete'}
    return candidate, bool(blocked or selection != 'accept')
