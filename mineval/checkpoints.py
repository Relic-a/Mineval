"""Durable preparation checkpoints, bound to current inputs and sealed evidence."""
import hashlib
from .artifacts import digest, inventory, read_json, write_json
from .memory import encoded
from . import review


class PreparationCheckpoints:
    def __init__(self, store, task, runtime):
        self.store, self.task, self.runtime = store, store.task(task), runtime
        self.draft = store.draft(task)
        self.path = self.task / 'preparation-progress.json'
        self.saved = read_json(self.path) if self.path.exists() else {}

    def state(self):
        source = read_json(self.task / 'source-hashes.json')
        # Verify preserved evidence, including history, before reusing its derivatives.
        actual = {}
        for name in ('base', 'future', 'diffs'):
            if (self.task / 'private' / name).exists():
                actual.update({name + '/' + p: v for p, v in inventory(self.task / 'private' / name).items()})
        history = self.task / 'private/history.pack'
        if history.exists():
            actual['history.pack'] = {'sha256': digest(history), 'executable': bool(history.stat().st_mode & 0o111)}
        if source != actual:
            raise ValueError('Stored source evidence was modified')
        config = read_json(self.draft / 'control/verify.json')
        return {'schema': 1, 'source': source, 'metadata': digest(self.task / 'metadata.json'),
                'workspace': inventory(self.draft / 'workspace'), 'config': config,
                'runtime': self.runtime(config['image'])}

    def run(self, name, inputs, action, verify, *, post_inputs=None):
        key = hashlib.sha256(encoded(inputs).encode()).hexdigest()
        old = self.saved.get(name)
        if old and old['key'] == key and verify(old['value']):
            return old['value']
        value = action()
        if not verify(value):
            # Failed/incomplete results never displace a completed checkpoint.
            return value
        if post_inputs:
            key = hashlib.sha256(encoded(post_inputs()).encode()).hexdigest()
        self.saved[name] = {'key': key, 'value': value}
        write_json(self.path, self.saved)
        return value

    def cleanup(self, value):
        attempt = self.task / 'cleanups' / value['cleanup_id']
        review.checked(attempt)
        return (value.get('disposition') == 'ready-for-review'
                and value == read_json(attempt / 'cleanup-report.json')
                and value == read_json(self.task / 'cleanup-latest.json')
                and inventory(self.draft / 'workspace') == read_json(attempt / 'tree-hashes.json')
                and read_json(self.draft / 'control/verify.json') == read_json(attempt / 'verify-config.json'))

    def diagnostic(self, value):
        path = self.task / 'diagnostics' / (value['diagnostic_id'] + '.json')
        if digest(path) != read_json(path.with_suffix('.sha256.json'))['sha256']:
            raise ValueError('Diagnostic receipt mismatch')
        return (value.get('status') in ('completed', 'checks_failed')
                and value == read_json(path) and value == read_json(self.task / 'verify-latest.json'))

    def current_diagnostic(self):
        path = self.task / 'verify-latest.json'
        if not path.exists():
            return None
        value = read_json(path)
        state = self.state()
        if (value.get('input_hashes') == state['workspace'] and value.get('configuration') == state['config']
                and value.get('image_id') == state['runtime'] and self.diagnostic(value)):
            return value
        return None

    def extraction(self, value):
        if not value.get('extraction_id'):
            return False
        review.checked(self.task / 'private/extractions' / value['extraction_id'])
        return True

    def validation(self, value):
        path = self.task / 'private/validations' / value['extraction_id'] / value['validation_id']
        review.checked(path)
        return value['interpretation'] != 'environment_failure'

    def approval(self, value):
        if value.get('verdict') != 'approve':
            return False
        review.approved(self.store, self.task.name, value['review_id'])
        return True
