"""Storage paths and immutable record primitives."""
import re
from pathlib import Path

from mineval.core.artifacts import digest, read_json, write_json


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value):
        raise ValueError('Invalid task/version identifier')
    return value


def commit(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value):
        raise ValueError('Historical commits must be full lowercase SHAs')
    return value


class RecordStore:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def task(self, task_id):
        return self.root / 'tasks' / identifier(task_id)

    def draft(self, task_id):
        return self.root / 'drafts' / identifier(task_id)

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
