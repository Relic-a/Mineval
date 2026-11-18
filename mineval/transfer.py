"""Integrity-checked private bundles for moving Mineval data between hosts.

SSH authenticates the peer and encrypts the stream. The manifest also detects
accidental corruption and makes a received bundle independently inspectable.
Bundles are staged before publication and never overwrite an existing file.
"""
import os
import re
from pathlib import Path
import shutil
import tempfile

from .artifacts import digest, inventory, lock, pack, unpack, relative, write_json, read_json


def create(source, archive, manifest):
    source, archive, manifest = Path(source), Path(archive), Path(manifest)
    files = inventory(source)
    pack(source, archive)
    if inventory(source) != files:
        raise ValueError('Bundle input changed while packing')
    receipt = {'schema_version': 1, 'archive_sha256': digest(archive), 'files': files}
    write_json(manifest, receipt)
    return receipt


def verify(archive, manifest, destination):
    """Unpack into a new directory and validate both archive and content hashes."""
    receipt = read_json(manifest)
    if (not isinstance(receipt, dict) or set(receipt) != {'schema_version', 'archive_sha256', 'files'}
            or receipt['schema_version'] != 1 or not isinstance(receipt['files'], dict)):
        raise ValueError('Invalid bundle receipt')
    if digest(archive) != receipt['archive_sha256']:
        raise ValueError('Bundle archive checksum mismatch')
    unpack(archive, destination)
    try:
        if inventory(destination) != receipt['files']:
            raise ValueError('Bundle contents do not match receipt')
    except BaseException:
        shutil.rmtree(destination)
        raise
    return receipt


def validate_inventory(files, *, prefixes=None):
    if not isinstance(files, dict) or len(files) > 100_000:
        raise ValueError('Invalid export inventory')
    links = {}
    for name, entry in files.items():
        relative(name)
        if prefixes is not None and not any(name.startswith(prefix) for prefix in prefixes):
            raise ValueError('Export inventory outside job scope')
        if isinstance(entry, dict) and set(entry) == {'symlink'} and isinstance(entry['symlink'], str):
            links[name] = entry['symlink']
            continue
        if (not isinstance(entry, dict) or set(entry) != {'sha256', 'executable'} or
                not isinstance(entry['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) or
                type(entry['executable']) is not bool):
            raise ValueError('Invalid export inventory entry')
    from .artifacts import validate_links
    validate_links(links)
    return files


def local_inventory(store, task, job_id):
    from .preparation import identifier
    task, job_id = identifier(task), identifier(job_id)
    result = {}
    with lock(store.root / 'locks' / (task + '.lock'), shared=True):
        marker = store.root / 'remote-imports' / (task + '.json')
        if marker.exists() and read_json(marker).get('job_id') != job_id:
            raise ValueError('Another remote preparation import needs recovery')
        for namespace, source in (('tasks', store.task(task)), ('drafts', store.draft(task))):
            if source.exists():
                result.update({f'{namespace}/{task}/{name}': value for name, value in inventory(source).items()})
    return result


def create_delta(source, archive, receipt, known, task):
    """Pack changed files, retaining the authoritative complete inventory."""
    from .preparation import identifier
    task = identifier(task)
    known = validate_inventory(known, prefixes=(f'tasks/{task}/', f'drafts/{task}/'))
    source = Path(source)
    full = inventory(source)
    reused = {name: value for name, value in full.items() if known.get(name) == value}
    for name in reused:
        (source / relative(name)).unlink()
    subset = create(source, archive, receipt)
    if subset['files'] != {name: value for name, value in full.items() if name not in reused}:
        raise ValueError('Delta export inventory changed')
    return {'schema_version': 1, 'full_inventory': full}


def reconstruct_delta(destination, descriptor, receipt, store, task, job_id):
    """Rebuild a verified subset using only currently identical local job files."""
    from .preparation import identifier
    task, job_id = identifier(task), identifier(job_id)
    if not isinstance(descriptor, dict) or set(descriptor) != {'schema_version', 'full_inventory'} or descriptor['schema_version'] != 1:
        raise ValueError('Invalid delta descriptor')
    full = validate_inventory(descriptor['full_inventory'])
    if any(not (name.startswith((f'tasks/{task}/', f'drafts/{task}/', f'resource-usage/{job_id}-'))
                    or name == 'observations.json') for name in full):
        raise ValueError('Export inventory outside job scope')
    subset = validate_inventory(receipt['files'])
    if any(full.get(name) != entry for name, entry in subset.items()):
        raise ValueError('Delta archive disagrees with full inventory')
    missing = {name: value for name, value in full.items() if name not in subset}
    prefixes = (f'tasks/{task}/', f'drafts/{task}/')
    validate_inventory(missing, prefixes=prefixes)
    destination = Path(destination)
    with lock(store.root / 'locks' / (task + '.lock'), shared=True):
        marker = store.root / 'remote-imports' / (task + '.json')
        if marker.exists() and read_json(marker).get('job_id') != job_id:
            raise ValueError('Another remote preparation import needs recovery')
        roots = {'tasks': store.task(task), 'drafts': store.draft(task)}
        before = {key: inventory(path) for key, path in roots.items() if path.exists()}
        for name, entry in missing.items():
            parts = relative(name).parts
            if before.get(parts[0], {}).get('/'.join(parts[2:])) != entry:
                raise ValueError('Reused local evidence changed or is missing')
        for name in missing:
            parts = relative(name).parts
            namespace = parts[0]
            source = roots[namespace].joinpath(*parts[2:])
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                raise ValueError('Unexpected file in delta archive')
            shutil.copy2(source, target, follow_symlinks=False)
        after = {key: inventory(path) for key, path in roots.items() if path.exists()}
        if before != after or inventory(destination) != full:
            raise ValueError('Reconstructed export inventory mismatch')
    return full


def import_benchmark(store, incoming, job):
    """Validate and import a remote attempt without replacing local evidence.

    Existing identical artifacts make a repeated import idempotent; any differing
    artifact is a conflict. The task lock prevents readers from seeing publication.
    """
    from . import benchmark, evaluation
    from .preparation import Store, identifier
    from .workflows import matches

    task = identifier(job['task'])
    payload = job['payload']
    run_id = identifier(payload['run_id'])
    evaluation_id = identifier(payload['evaluation_id'])
    source = Store(incoming)
    with source.operation(task, allow_rejected=True, shared=True):
        run = benchmark.inspect_attempt(source, task, run_id)
        if (run.get('prepared_revision') != payload['revision'] or
                not matches(run, payload['revision'], payload['configuration'])):
            raise ValueError('Remote attempt identity mismatch')
        evaluation_path = source.task(task) / 'evaluations' / (evaluation_id + '.json')
        if evaluation_path.exists():
            assessed = evaluation.inspect(source, task, evaluation_id)
            if assessed['run_id'] != run_id:
                raise ValueError('Remote evaluation identity mismatch')
        items = [('run-outputs', run_id), ('runs', run_id + '.json'), ('runs', run_id + '.sha256')]
        if evaluation_path.exists():
            items += [('evaluation-evidence', evaluation_id), ('evaluations', evaluation_id + '.json'),
                      ('evaluations', evaluation_id + '.sha256')]
        pairs = [(source.task(task) / namespace / name, store.task(task) / namespace / name)
                 for namespace, name in items]
        pairs = [(src, dst) for src, dst in pairs if src.exists()]
        with store.operation(task, allow_rejected=True):
            if store._inspect_revision(task, payload['revision']['version']) != payload['revision']:
                raise ValueError('Local frozen revision differs from remote attempt')
            with tempfile.TemporaryDirectory(dir=store.root) as temp:
                staged = []
                for index, (src, dst) in enumerate(pairs):
                    if src.is_symlink():
                        raise ValueError('Linked artifact is forbidden')
                    if dst.exists():
                        if src.is_dir() != dst.is_dir() or (inventory(src) != inventory(dst) if src.is_dir() else digest(src) != digest(dst)):
                            raise ValueError('Remote artifact conflicts with local evidence')
                        continue
                    target = Path(temp) / str(index)
                    if src.is_dir():
                        from .artifacts import copy_tree
                        copy_tree(src, target)
                    else:
                        shutil.copy2(src, target, follow_symlinks=False)
                        if digest(src) != digest(target):
                            raise ValueError('Artifact changed during import')
                    staged.append((target, dst))
                for src, dst in staged:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(src, dst)


def benchmark_input(store, job, destination, settings):
    """Create a minimal private worker input pinned to one frozen revision."""
    from .artifacts import copy_tree
    from .preparation import identifier
    task = identifier(job['task'])
    payload = job['payload']
    version = identifier(payload['revision']['version'])
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    try:
        with store.operation(task, shared=True):
            if store._inspect_revision(task, version) != payload['revision']:
                raise ValueError('Frozen revision changed before transfer')
            target = destination / 'data/tasks' / task
            (target / 'versions').mkdir(parents=True)
            for name in ('metadata.json', 'status.json'):
                shutil.copy2(store.task(task) / name, target / name, follow_symlinks=False)
            copy_tree(store.task(task) / 'versions' / version, target / 'versions' / version)
            for name in ('runs', 'run-outputs', 'evaluations', 'evaluation-evidence'):
                (target / name).mkdir()
        write_json(destination / 'job.json', job)
        if (store.root / 'model-reviews.sqlite3').exists():
            from .memory import Memory
            history = Memory(store).history(payload['configuration']['model'])
            write_json(destination / 'prior-observations.json', [
                {key: item[key] for key in ('model', 'body', 'sources', 'event_key')} for item in history])
        # Only execution settings travel; paths and model credentials never do.
        allowed = ('concurrency', 'model_concurrency', 'resources', 'evaluator_agent',
                   'evaluator_timeout_seconds', 'qualitative_review', 'review_image',
                   'preparation_image', 'review_model', 'review_effort')
        write_json(destination / 'settings.json', {key: settings[key] for key in allowed if key in settings})
        inventory(destination)
    except BaseException:
        shutil.rmtree(destination)
        raise


def preparation_input(store, job, destination, settings, evaluation_config=None):
    """Stage a task and draft, including its private historical evidence."""
    from .artifacts import copy_tree
    from .preparation import identifier
    task = identifier(job['task'])
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    try:
        with store.operation(task, shared=True):
            target = destination / 'data'
            (target / 'tasks').mkdir(parents=True)
            (target / 'drafts').mkdir()
            before = {'task': inventory(store.task(task)), 'draft': inventory(store.draft(task))}
            copy_tree(store.task(task), target / 'tasks' / task, expected_inventory=before['task'])
            copy_tree(store.draft(task), target / 'drafts' / task, expected_inventory=before['draft'])
        write_json(destination / 'baseline.json', before)
        write_json(destination / 'job.json', job)
        allowed = ('concurrency', 'model_concurrency', 'resources', 'preparation_image',
                   'review_model', 'review_effort')
        write_json(destination / 'settings.json', {key: settings[key] for key in allowed if key in settings})
        if evaluation_config is not None:
            write_json(destination / 'evaluation-config.json', evaluation_config)
        inventory(destination)
    except BaseException:
        shutil.rmtree(destination)
        raise


def import_preparation(store, incoming, job, baseline):
    """Reconcile remote preparation and draft against their original inventories.

    A durable marker blocks unrelated task operations during a partially published
    import. Repeating the same import accepts only baseline or remote bytes.
    """
    from .artifacts import lock
    from .preparation import Store, identifier
    from .review import checked
    task = identifier(job['task'])
    job_id = identifier(job['id'])
    source = Store(incoming)
    with source.operation(task, allow_rejected=True, shared=True):
        remote_task, remote_draft = source.task(task), source.draft(task)
        status = read_json(remote_task / 'status.json')
        if status.get('status') == 'frozen':
            revision = source._inspect_revision(task, status['version'])
            if revision['snapshot_sha256'] != status['snapshot_sha256']:
                raise ValueError('Remote frozen status does not match revision')
        else:
            revision = None
        remote_files = {'task': inventory(remote_task), 'draft': inventory(remote_draft)}
        for pattern in ('versions/*/artifact-hashes.json', 'cleanups/*/artifact-hashes.json',
                        'private/extractions/*/artifact-hashes.json', 'private/reviews/*/artifact-hashes.json',
                        'private/extraction-attempts/*/artifact-hashes.json',
                        'private/validations/*/*/artifact-hashes.json'):
            for manifest in remote_task.glob(pattern):
                checked(manifest.parent)
        old = baseline['task']
        # Checkpoints are a mutable index into sealed evidence, not a receipt.
        # The three-way reconciliation below still rejects concurrent local edits.
        mutable = {'status.json', 'cleanup-latest.json', 'verify-latest.json', 'preparation-progress.json'}
        for name, value in old.items():
            if name not in remote_files['task'] or (name not in mutable and remote_files['task'][name] != value):
                raise ValueError('Remote preparation changed existing evidence')
        marker = store.root / 'remote-imports' / (task + '.json')
        with lock(store.root / 'locks' / (task + '.lock')):
            roots = {'task': store.task(task), 'draft': store.draft(task)}
            if not roots['task'].is_dir() or not roots['draft'].is_dir():
                raise ValueError('Local task or draft is unavailable')
            if marker.exists() and read_json(marker).get('job_id') != job_id:
                raise ValueError('Another remote preparation import needs recovery')
            local = {key: inventory(root) for key, root in roots.items()}
            for key in ('task', 'draft'):
                for name in set(local[key]) | set(baseline[key]) | set(remote_files[key]):
                    before, current, after = baseline[key].get(name), local[key].get(name), remote_files[key].get(name)
                    if current != before and current != after:
                        raise ValueError('Local ' + key + ' changed during remote preparation')
            with tempfile.TemporaryDirectory(dir=store.root) as temp:
                staged = []
                for key, source_root in (('task', remote_task), ('draft', remote_draft)):
                    for name, value in remote_files[key].items():
                        if local[key].get(name) == value:
                            continue
                        target = Path(temp) / str(len(staged))
                        shutil.copy2(source_root / name, target, follow_symlinks=False)
                        if ((not target.is_symlink() or os.readlink(target) != value['symlink'])
                                if 'symlink' in value else digest(target) != value['sha256']):
                            raise ValueError('Remote preparation changed during import')
                        staged.append((key, name, target))
                # Keep the old state visible until every other file is published.
                staged.sort(key=lambda item: item[0] == 'task' and item[1] == 'status.json')
                write_json(marker, {'job_id': job_id})
                for key in ('task', 'draft'):
                    for name in set(local[key]) - set(remote_files[key]):
                        (roots[key] / name).unlink()
                for key, name, staged_file in staged:
                    destination = roots[key] / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.is_symlink() and destination.is_dir():
                        # A draft may replace a directory with a file. Remove only
                        # empty directories left by the verified deletions above.
                        for current, dirs, files in os.walk(destination, topdown=False):
                            if files:
                                raise ValueError('Local directory changed during import')
                            Path(current).rmdir()
                    os.replace(staged_file, destination)
                if any(inventory(roots[key]) != remote_files[key] for key in ('task', 'draft')):
                    raise ValueError('Imported preparation failed integrity verification')
                if revision and store._inspect_revision(task, status['version']) != revision:
                    raise ValueError('Imported revision failed verification')
                marker.unlink()
        return revision
