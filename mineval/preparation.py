"""External-candidate intake and immutable, versioned preparation artifacts."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from uuid import uuid4

from .artifacts import copy_tree, digest, inventory, lock, pack, read_json, relative, unpack, write_json


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value):
        raise ValueError('Invalid task/version identifier')
    return value


def commit(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value):
        raise ValueError('Historical commits must be full lowercase SHAs')
    return value


def git(repo, *args, output=None):
    with tempfile.TemporaryFile() as err:
        try:
            result = subprocess.run(['git', '--no-replace-objects', '-C', str(repo), *args],
                                    stdout=output or subprocess.PIPE, stderr=err,
                                    stdin=subprocess.DEVNULL, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f'Cannot read repository: {exc}') from exc
        if result.returncode:
            err.seek(0)
            raise ValueError('Git source error: ' + err.read(4096).decode(errors='replace'))
    return result.stdout


def export_git(repo, sha, target):
    if git(repo, 'rev-parse', '--verify', sha + '^{commit}').decode().strip() != sha:
        raise ValueError('Commit identity mismatch')
    # Read blobs directly: git archive honors export-ignore and could omit task files.
    entries = []
    from .artifacts import MAX_BYTES, MAX_FILES
    for entry in git(repo, 'ls-tree', '-rz', '--full-tree', sha).split(b'\0'):
        if not entry:
            continue
        header, name = entry.split(b'\t', 1)
        mode, kind, oid = header.split()
        if mode not in (b'100644', b'100755') or kind != b'blob':
            raise ValueError('Symlinks/submodules require an explicit supported producer export')
        path = relative(name.decode('utf-8'))
        entries.append((mode, oid, path))
        if len(entries) > MAX_FILES:
            raise ValueError('Repository exceeds file budget')
    # Bound the batch before exporting blob contents to temporary disk.
    probe = subprocess.run(['git', '--no-replace-objects', '-C', str(repo), 'cat-file', '--batch-check'],
                           input=b''.join(oid + b'\n' for _, oid, _ in entries),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if probe.returncode or sum(int(line.split()[2]) for line in probe.stdout.splitlines()) > MAX_BYTES:
        raise ValueError('Repository exceeds artifact budget or contains missing objects')
    target = Path(target)
    target.mkdir(parents=True)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.run(['git', '--no-replace-objects', '-C', str(repo), 'cat-file', '--batch'],
                              input=b''.join(oid + b'\n' for _, oid, _ in entries), stdout=out,
                              stderr=err, timeout=120)
        if proc.returncode:
            raise ValueError('Could not export Git objects')
        out.seek(0)
        total = 0
        for mode, oid, path in entries:
            object_id, kind, size = out.readline().split()
            size = int(size)
            total += size
            if object_id != oid or kind != b'blob' or total > MAX_BYTES:
                raise ValueError('Invalid or oversized Git export')
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('xb') as dst:
                remaining = size
                while remaining:
                    block = out.read(min(remaining, 1024 * 1024))
                    if not block:
                        raise ValueError('Truncated Git object')
                    dst.write(block)
                    remaining -= len(block)
            if out.read(1) != b'\n':
                raise ValueError('Invalid Git object separator')
            destination.chmod(0o755 if mode == b'100755' else 0o644)


class Store:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def task(self, task_id):
        return self.root / 'tasks' / identifier(task_id)

    def draft(self, task_id):
        return self.root / 'drafts' / identifier(task_id)

    def list_tasks(self):
        """The task pool grows independently of any published suite."""
        from .categories import category_for
        pool = self.root / 'tasks'
        if not pool.exists():
            return []
        result = []
        for task in sorted(pool.iterdir()):
            with lock(self.root / 'locks' / (identifier(task.name) + '.lock')):
                metadata = read_json(task / 'metadata.json')
                result.append({key: metadata[key] for key in ('task_id', 'repository', 'task_summary')} |
                              {'category': category_for(metadata),
                               'state': read_json(task / 'status.json')})
        return result

    def inspect_task(self, task_id):
        # Rejected tasks remain inspectable; operation() intentionally blocks mutation.
        task = self.task(task_id)
        with lock(self.root / 'locks' / (identifier(task_id) + '.lock')):
            if not task.is_dir():
                raise ValueError('Task is not registered')
            return {'metadata': read_json(task / 'metadata.json'),
                    'state': read_json(task / 'status.json'),
                    'cleanup': read_json(task / 'cleanup-latest.json') if (task / 'cleanup-latest.json').exists() else None,
                    'private_extractions': sorted(p.name for p in (task / 'private/extractions').glob('*')),
                    'private_reviews': sorted(p.name for p in (task / 'private/reviews').glob('*')),
                    'extraction_attempts': sorted(p.name for p in (task / 'private/extraction-attempts').glob('*')),
                    'prepared_revisions': sorted(p.name for p in (task / 'versions').iterdir())
                    if (task / 'versions').exists() else [],
                    'runs': sorted(p.name for p in (task / 'runs').glob('*.json')),
                    'evaluations': sorted(p.name for p in (task / 'evaluations').glob('*.json'))}

    def inspect_revision(self, task_id, version):
        with self.operation(task_id, allow_rejected=True):
            return self._inspect_revision(task_id, version)

    def list_suites(self):
        return [self.inspect_suite(p.name) for p in sorted((self.root / 'suites').glob('*'))]

    def reserve_run(self, task_id, run_id, version, *, model, harness):
        """Persist identity only. This neither invokes a harness nor claims a result."""
        for label, identity in (('model', model), ('harness', harness)):
            if (not isinstance(identity, dict) or set(identity) != {'name', 'version', 'configuration'}
                    or not isinstance(identity['name'], str) or not identity['name'].strip()
                    or not isinstance(identity['version'], str) or not identity['version'].strip()
                    or not isinstance(identity['configuration'], dict)):
                raise ValueError(f'{label} requires name, version and configuration')
        with self.operation(task_id):
            return self._reserve_record(task_id, 'runs', run_id, {
                'schema_version': 1, 'task_id': task_id, 'run_id': identifier(run_id),
                'prepared_revision': self._inspect_revision(task_id, version),
                'model': model, 'harness': harness, 'status': 'reserved'})

    def reserve_evaluation(self, task_id, evaluation_id, run_id):
        """Legacy reservation primitive; new assessments pin sealed outputs too."""
        with self.operation(task_id):
            self._inspect_record(task_id, 'runs', run_id)
            return self._reserve_record(task_id, 'evaluations', evaluation_id, {
                'schema_version': 1, 'task_id': task_id, 'evaluation_id': identifier(evaluation_id),
                'run_id': identifier(run_id),
                'run_sha256': digest(self.task(task_id) / 'runs' / (run_id + '.json')),
                'status': 'reserved'})

    def _reserve_record(self, task_id, namespace, record_id, record):
        path = self.task(task_id) / namespace / (identifier(record_id) + '.json')
        receipt = path.with_suffix('.sha256')
        if path.exists() or receipt.exists():
            raise ValueError('Record ID already exists; reserve a new ID')
        write_json(path, record)
        write_json(receipt, {'sha256': digest(path)})
        path.chmod(0o444)
        receipt.chmod(0o444)
        return read_json(path)

    def _inspect_record(self, task_id, namespace, record_id):
        path = self.task(task_id) / namespace / (identifier(record_id) + '.json')
        if not path.is_file():
            raise ValueError('Record does not exist')
        if digest(path) != read_json(path.with_suffix('.sha256'))['sha256']:
            raise ValueError('Record artifact hash mismatch')
        return read_json(path)

    def inspect_run(self, task_id, run_id):
        with self.operation(task_id, allow_rejected=True):
            from .benchmark import inspect_attempt
            return inspect_attempt(self, task_id, run_id)

    def inspect_evaluation(self, task_id, evaluation_id):
        with self.operation(task_id, allow_rejected=True):
            from .evaluation import inspect
            return inspect(self, task_id, evaluation_id)

    def _inspect_revision(self, task_id, version):
        frozen = self.task(task_id) / 'versions' / identifier(version)
        if not frozen.is_dir():
            raise ValueError('Prepared revision does not exist')
        actual = inventory(frozen)
        actual.pop('artifact-hashes.json', None)
        if actual != read_json(frozen / 'artifact-hashes.json'):
            raise ValueError('Prepared revision artifact hash mismatch')
        sha = digest(frozen / 'snapshot.tar.gz')
        if sha != (frozen / 'snapshot.sha256').read_text().strip():
            raise ValueError('Snapshot hash mismatch')
        return {'task_id': task_id, 'version': version, 'snapshot_sha256': sha,
                'artifact_manifest_sha256': digest(frozen / 'artifact-hashes.json')}

    def create_suite(self, definition):
        """Publish an explicit, ordered membership; never resolve a mutable 'latest'."""
        if not isinstance(definition, dict) or set(definition) - {'suite_id', 'description', 'members'}:
            raise ValueError('Suite requires suite_id, members and optional description')
        suite_id = identifier(definition['suite_id'])
        members = definition.get('members')
        if not isinstance(members, list) or not members:
            raise ValueError('Suite members must be a nonempty list of task_id/version pairs')
        if 'description' in definition and not isinstance(definition['description'], str):
            raise ValueError('Suite description must be a string')
        with lock(self.root / 'locks' / 'suites' / (suite_id + '.lock')):
            target = self.root / 'suites' / suite_id
            if target.exists():
                raise ValueError('Suite already exists; publish a new suite ID')
            pinned, seen = [], set()
            for member in members:
                if not isinstance(member, dict) or set(member) != {'task_id', 'version'}:
                    raise ValueError('Each suite member requires exactly task_id and version')
                task_id = identifier(member['task_id'])
                if task_id in seen:
                    raise ValueError('Duplicate task in suite')
                seen.add(task_id)
                with self.operation(task_id):
                    pinned.append(self._inspect_revision(task_id, member['version']))
            record = {'schema_version': 1, 'suite_id': suite_id,
                      'description': definition.get('description', ''), 'members': pinned}
            with tempfile.TemporaryDirectory(dir=self.root) as temp:
                staged = Path(temp) / 'suite'
                write_json(staged / 'suite.json', record)
                write_json(staged / 'artifact-hashes.json', inventory(staged))
                for path in staged.iterdir():
                    path.chmod(0o444)
                target.parent.mkdir(exist_ok=True)
                os.rename(staged, target)
            return record

    def inspect_suite(self, suite_id):
        target = self.root / 'suites' / identifier(suite_id)
        if not target.is_dir():
            raise ValueError('Suite does not exist')
        actual = inventory(target)
        actual.pop('artifact-hashes.json', None)
        if actual != read_json(target / 'artifact-hashes.json'):
            raise ValueError('Suite artifact hash mismatch')
        return read_json(target / 'suite.json')

    @contextmanager
    def operation(self, task_id, *, allow_rejected=False):
        with lock(self.root / 'locks' / (identifier(task_id) + '.lock')):
            if not self.task(task_id).is_dir():
                raise ValueError('Task is not registered')
            if not allow_rejected and read_json(self.task(task_id) / 'status.json')['status'] == 'rejected':
                raise ValueError('Task has been rejected')
            if not allow_rejected and read_json(self.task(task_id) / 'status.json')['status'] == 'needs-review':
                raise ValueError('Incomplete candidate needs review before preparation')
            if not allow_rejected and read_json(self.task(task_id) / 'status.json')['status'] == 'retired':
                raise ValueError('Task is retired; historical records remain inspectable')
            yield

    def register(self, candidate):
        from .intake import normalize
        candidate, deferred = normalize(candidate)
        task_id = identifier(candidate['task_id'])
        base = candidate.get('base_commit')
        futures = candidate['future_commits']
        source = candidate.get('base_source')
        task, draft = self.task(task_id), self.draft(task_id)
        with lock(self.root / 'locks' / (task_id + '.lock')):
            if task.exists() or draft.exists():
                raise ValueError('Task already registered')
            task.mkdir(parents=True, mode=0o700)
            try:
                if deferred:
                    state = 'rejected' if candidate.get('selection_status') == 'reject' else 'needs-review'
                    write_json(task / 'metadata.json', candidate | {'grounding': 'not-imported'})
                    write_json(task / 'status.json', {'status': state})
                    write_json(task / 'artifact-hashes.json', {'metadata.json': {'sha256': digest(task / 'metadata.json')}})
                    return {'task_id': task_id, 'status': state, 'intake': candidate['intake'],
                            'grounding': 'not-imported'}
                private = task / 'private'
                private.mkdir(mode=0o700)
                if source['type'] == 'git':
                    repo = Path(source['path']).resolve(strict=True)
                    export_git(repo, base, private / 'base')
                    from .history import create_pack
                    create_pack(repo, base, private / 'history.pack')
                    for sha in futures:
                        git(repo, 'merge-base', '--is-ancestor', base, sha)
                        export_git(repo, sha, private / 'future' / sha)
                        patch = private / 'diffs' / (sha + '.patch')
                        patch.parent.mkdir(exist_ok=True)
                        with patch.open('wb') as out:
                            git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--binary', base, sha, '--', output=out)
                        inventory(private)
                    grounding = 'git-objects-and-ancestry-checked'
                elif source['type'] in ('tree', 'archive'):
                    if not candidate.get('source_attestation') or not candidate.get('future_source'):
                        raise ValueError('Raw exports require source_attestation and future_source')
                    self._import(source, private / 'base')
                    self._import(candidate['future_source'], private / 'future')
                    for sha in futures:
                        if not (private / 'future' / sha).is_dir():
                            raise ValueError('Raw future_source must contain one directory per future SHA')
                    grounding = 'producer-attested-raw-export'
                else:
                    raise ValueError('Supported sources: git, tree, archive')
                for evidence in candidate.get('future_evidence', []):
                    if 'path' in evidence and not (private / 'future' / evidence['commit'] / evidence['path']).is_file():
                        raise ValueError('Future evidence path does not exist at its source commit')
                base_inventory = inventory(private / 'base')
                source_inventory = inventory(private)
                write_json(task / 'metadata.json', candidate | {'future_commits': futures, 'grounding': grounding})
                write_json(task / 'source-hashes.json', source_inventory)
                write_json(task / 'base-inventory.json', base_inventory)
                write_json(task / 'status.json', {'status': 'draft'})
                for directory in ('runs', 'evaluations'):
                    (task / directory).mkdir()
                copy_tree(private / 'base', draft / 'workspace')
                (draft / 'control').mkdir()
                write_json(draft / 'control' / 'task.json', {
                    key: candidate[key] for key in ('task_id', 'repository', 'base_commit', 'task_summary')})
                write_json(draft / 'control' / 'verify.json', {
                    'image': '', 'network': 'bridge', 'workdir': '/workspace',
                    'limits': {'timeout_seconds': 300},
                    'commands': []})
                write_json(task / 'artifact-hashes.json', {name: {'sha256': digest(task / name)} for name in ('metadata.json', 'source-hashes.json', 'base-inventory.json')})
            except BaseException:
                shutil.rmtree(task)
                shutil.rmtree(draft, ignore_errors=True)
                raise
        return {'task_id': task_id, 'status': 'draft', 'grounding': grounding, 'intake': candidate['intake']}

    @staticmethod
    def _import(source, target):
        if source['type'] == 'tree':
            copy_tree(Path(source['path']).resolve(strict=True), target)
        elif source['type'] == 'archive':
            unpack(source['path'], target)
        else:
            raise ValueError('Raw source type must be tree or archive')

    def reject(self, task_id, reason):
        if not reason.strip():
            raise ValueError('Rejection requires a reason')
        with self.operation(task_id):
            result = {'status': 'rejected', 'reason': reason}
            task = self.task(task_id)
            record = task / 'rejections' / (uuid4().hex + '.json')
            write_json(record, result)
            write_json(record.with_suffix('.sha256.json'), {'sha256': digest(record)})
            write_json(task / 'status.json', result)
            return result

    def retire(self, task_id, reason):
        """Exclude future work without modifying frozen artifacts or prior results."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('Retirement requires a reason')
        with self.operation(task_id, allow_rejected=True):
            task = self.task(task_id)
            previous = read_json(task / 'status.json')
            if previous['status'] == 'retired':
                raise ValueError('Task is already retired')
            from .benchmark import now
            result = {'status': 'retired', 'reason': reason.strip(), 'retired_at': now(),
                      'previous_state': previous}
            record = task / 'retirements' / (uuid4().hex + '.json')
            write_json(record, result)
            write_json(record.with_suffix('.sha256.json'), {'sha256': digest(record)})
            write_json(task / 'status.json', result)
            return result

    def finalize(self, task_id, review=None, *, review_id=None):
        with self.operation(task_id), tempfile.TemporaryDirectory(dir=self.root) as temp:
            task, draft = self.task(task_id), self.draft(task_id)
            from .review import approved
            if not review_id:
                raise ValueError('Require an approved independent review session ID; inline attestations cannot freeze new revisions')
            approval, review_directory = approved(self, task_id, review_id)
            review = approval['report'] | {'reviewer': approval['producer']['session_id']}
            if review.get('objective_preserved') is not True or review.get('snapshot_reviewed') is not True or not review.get('reviewer'):
                raise ValueError('Require reviewer and explicit objective/snapshot review attestations')
            # Only host-owned operations hold this lock; active sessions also hold it.
            clean = Path(temp) / 'tree'
            current = copy_tree(draft / 'workspace', clean)
            before = read_json(task / 'base-inventory.json')
            if inventory(task / 'private' / 'base') != before:
                raise ValueError('Stored base provenance was modified')
            changed = sorted(p for p in before.keys() | current.keys() if before.get(p) != current.get(p))
            reasons = {x['path']: x['reason'] for x in review.get('changes', []) if x.get('reason', '').strip()}
            if len(reasons) != len(review.get('changes', [])) or set(reasons) != set(changed):
                raise ValueError('Review must give a reason for exactly every changed/deleted/added path: ' + repr(changed))
            version = uuid4().hex
            staged = Path(temp) / version
            staged.mkdir()
            history = task / 'private' / 'history.pack'
            history_format = {'format': 'none'}
            if history.exists():
                expected = read_json(task / 'source-hashes.json')['history.pack']['sha256']
                if digest(history) != expected:
                    raise ValueError('Stored historical Git pack was modified')
                shutil.copyfile(history, staged / 'history.pack')
                metadata = read_json(task / 'metadata.json')
                history_format = {'format': 'base-pack-v1', 'path': 'history.pack',
                                  'base_commit': metadata['base_commit']}
            write_json(staged / 'workspace-format.json', {
                'schema_version': 1, 'tree': {'format': 'tar', 'path': 'snapshot.tar.gz'},
                'git_history': history_format})
            pack(clean, staged / 'snapshot.tar.gz')
            sha = digest(staged / 'snapshot.tar.gz')
            (staged / 'snapshot.sha256').write_text(sha + '\n')
            with (staged / 'cleanup-changes.patch').open('wb') as out:
                result = subprocess.run(['git', 'diff', '--no-index', '--no-ext-diff', '--binary',
                                         '--src-prefix=base/', '--dst-prefix=clean/',
                                         str(task / 'private' / 'base'), str(clean)],
                                        stdout=out, stderr=subprocess.PIPE, timeout=120)
                if result.returncode not in (0, 1):
                    raise ValueError('Cannot record cleanup patch: ' + result.stderr.decode(errors='replace'))
            write_json(staged / 'cleanup-report.json', review | {'changed_paths': changed, 'snapshot_sha256': sha})
            write_json(staged / 'tree-hashes.json', current)
            write_json(staged / 'task-instructions.json', {
                'task_summary': read_json(task / 'metadata.json')['task_summary']})
            # Config validation is shared with diagnostics.
            from .verification import load_config
            config = load_config(draft / 'control' / 'verify.json')
            write_json(staged / 'verify-config.json', config)
            cleanup = task / 'cleanup-latest.json'
            if cleanup.exists():
                record = read_json(cleanup)
                attempt = task / 'cleanups' / identifier(record['cleanup_id'])
                actual = inventory(attempt)
                actual.pop('artifact-hashes.json', None)
                if actual != read_json(attempt / 'artifact-hashes.json'):
                    raise ValueError('Cleanup artifact hash mismatch')
                if (record != read_json(attempt / 'cleanup-report.json') or
                        record['disposition'] != 'ready-for-review' or
                        current != read_json(attempt / 'tree-hashes.json') or
                        config != read_json(attempt / 'verify-config.json')):
                    raise ValueError('Cleanup requires revision/review or is stale; rerun cleanup before finalization')
                shutil.copyfile(attempt / 'cleanup-report.json', staged / 'cleanup-attempt.json')
            if not (task / 'verify-latest.json').exists():
                raise ValueError('Run a fresh diagnostic before finalization')
            diagnostic = read_json(task / 'verify-latest.json')
            if (diagnostic.get('status') not in ('completed', 'checks_failed')
                    or diagnostic.get('input_hashes') != current or diagnostic.get('configuration') != config
                    or not any(x.get('status') in ('passed', 'failed') for x in diagnostic.get('commands', []))):
                raise ValueError('Require a current diagnostic with a runnable command; task-related test failures are allowed')
            shutil.copyfile(task / 'verify-latest.json', staged / 'last-diagnostic.json')
            copy_tree(review_directory, staged / 'private/review')
            copy_tree(task / 'private/extractions' / approval['inputs']['extraction_id'],
                      staged / 'private/verification')
            validations = task / 'private/validations' / approval['inputs']['extraction_id']
            if validations.exists():
                copy_tree(validations, staged / 'private/validations')
            write_json(staged / 'approval.json', approval)
            write_json(staged / 'artifact-hashes.json', inventory(staged))
            versions = task / 'versions'
            versions.mkdir(exist_ok=True)
            os.rename(staged, versions / version)
            for p in (versions / version).rglob('*'):
                if p.is_file():
                    p.chmod(0o444)
            write_json(task / 'status.json', {'status': 'frozen', 'version': version, 'snapshot_sha256': sha})
            return {'version': version, 'snapshot_sha256': sha}

    def materialize(self, task_id, version, destination):
        with self.operation(task_id):
            return self._materialize(task_id, version, destination)

    def _materialize(self, task_id, version, destination):
        destination = Path(destination).resolve()
        for reserved in ('tasks', 'drafts', 'locks', 'suites'):
            protected = (self.root / reserved).resolve()
            if destination == protected or protected in destination.parents:
                raise ValueError('Workspace destination must be outside protected preparation artifacts')
        version = identifier(version)
        frozen = self.task(task_id) / 'versions' / version
        self._inspect_revision(task_id, version)
        # Legacy revisions have the same tree-only format without a descriptor.
        # Future history formats must be explicitly supported, never silently dropped.
        descriptor = frozen / 'workspace-format.json'
        fmt = read_json(descriptor) if descriptor.exists() else {
            'schema_version': 1, 'tree': {'format': 'tar', 'path': 'snapshot.tar.gz'},
            'git_history': {'format': 'none'}}
        history_format = fmt.get('git_history', {})
        allowed = {'format': 'none'}
        if history_format.get('format') == 'base-pack-v1':
            allowed = {'format': 'base-pack-v1', 'path': 'history.pack',
                       'base_commit': commit(history_format.get('base_commit'))}
        if fmt != {'schema_version': 1,
                   'tree': {'format': 'tar', 'path': 'snapshot.tar.gz'}, 'git_history': allowed}:
            raise ValueError('Unsupported workspace format; upgrade Mineval to materialize this revision')
        if digest(frozen / 'snapshot.tar.gz') != (frozen / 'snapshot.sha256').read_text().strip():
            raise ValueError('Snapshot hash mismatch')
        unpack(frozen / 'snapshot.tar.gz', destination)
        if inventory(destination) != read_json(frozen / 'tree-hashes.json'):
            shutil.rmtree(destination)
            raise ValueError('Extracted tree hash mismatch')
        if history_format.get('format') == 'base-pack-v1':
            from .history import restore
            try:
                restore(frozen / 'history.pack', history_format['base_commit'], destination)
            except BaseException:
                shutil.rmtree(destination)
                raise
        return {'workspace': str(Path(destination).absolute()), 'version': version}

    def extract(self, task_id, selections):
        """Save explicitly selected evidence. Never apply it to a starting tree."""
        with self.operation(task_id):
            return self._extract(task_id, selections)

    def _extract(self, task_id, selections, *, plan=None, producer_session=None):
        with tempfile.TemporaryDirectory(dir=self.root) as temp:
            task = self.task(task_id)
            metadata = read_json(task / 'metadata.json')
            original = read_json(task / 'source-hashes.json')
            records = []
            staging = Path(temp) / 'extraction'
            staging.mkdir()
            for item in selections:
                sha, path = commit(item['commit']), relative(item['path'])
                if sha not in metadata['future_commits'] or not item.get('purpose') or item.get('test_only') is not True:
                    raise ValueError('Extraction requires a known source commit, purpose and test_only attestation')
                key = f'future/{sha}/{path}'
                source = task / 'private' / key
                if key not in original or source.is_symlink() or digest(source) != original[key]['sha256']:
                    raise ValueError('Missing or modified source evidence')
                destination = staging / 'verification' / sha / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ValueError('Duplicate extraction entry')
                shutil.copyfile(source, destination)
                destination.chmod(0o755 if original[key]['executable'] else 0o644)
                records.append(item | {'sha256': digest(destination), 'stored_path': destination.relative_to(staging).as_posix()})
            if not records:
                raise ValueError('Select at least one verification artifact')
            write_json(staging / 'extraction-manifest.json', {'artifacts': records, 'applied': False,
                'plan': plan, 'producer_session': producer_session})
            write_json(staging / 'artifact-hashes.json', inventory(staging))
            extraction_id = uuid4().hex
            target = task / 'private' / 'extractions' / extraction_id
            target.parent.mkdir(exist_ok=True)
            os.rename(staging, target)
            for path in target.rglob('*'):
                if path.is_file():
                    path.chmod(0o444)
            return {'extraction_id': extraction_id, 'artifacts': records}
