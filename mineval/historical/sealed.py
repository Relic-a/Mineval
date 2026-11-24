"""Immutable private evidence receipts."""
from mineval.core.artifacts import digest, inventory, read_json, write_json


def checked(directory):
    actual = inventory(directory)
    actual.pop('artifact-hashes.json', None)
    if actual != read_json(directory / 'artifact-hashes.json'):
        raise ValueError('Private preparation artifact hash mismatch')
    return digest(directory / 'artifact-hashes.json')


def seal(staged, target):
    write_json(staged / 'artifact-hashes.json', inventory(staged))
    target.parent.mkdir(parents=True, exist_ok=True)
    staged.rename(target)
    for path in target.rglob('*'):
        if not path.is_symlink() and path.is_file():
            path.chmod(0o444)


def strings(value):
    return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)
