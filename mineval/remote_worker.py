"""Per-job remote execution using the normal historical workflow coordinator.

The SSH-facing caller supplies a verified bundle in a private worker directory.
Each job owns a disposable execution store; the originating store remains the
only authoritative store. A job identity is never started twice.
"""
from pathlib import Path

from .artifacts import read_json, write_json
from .preparation import identifier
from .transfer import verify


def initialize(root, job_id, archive, receipt):
    root, job_id = Path(root), identifier(job_id)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = root / job_id
    # An existing identity is never removed or reinitialized, even after a failed
    # upload: a disconnected sender must reconcile it instead of repeating work.
    directory.mkdir(mode=0o700)
    write_json(directory / 'state.json', {'state': 'receiving', 'job_id': job_id})
    verify(archive, receipt, directory / 'input')
    job = read_json(directory / 'input' / 'job.json')
    if (not isinstance(job, dict) or job.get('id') != job_id or
            job.get('kind') not in ('prepare', 'benchmark') or
            not isinstance(job.get('payload'), dict)):
        raise ValueError('Invalid remote job')
    identifier(job.get('task'))
    settings = read_json(directory / 'input' / 'settings.json')
    if not isinstance(settings, dict):
        raise ValueError('Invalid remote settings')
    write_json(directory / 'state.json', {'state': 'ready', 'job_id': job_id})
    return directory


