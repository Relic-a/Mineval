"""Local fake agents that write the same reports as isolated executables."""
from contextlib import contextmanager
from unittest.mock import Mock
from mineval.core.artifacts import write_json

class ReportAgent:
    def __init__(self, filename, report):
        self.filename, self.report = filename, report
        self.specs = []

    @contextmanager
    def container(self, spec):
        self.specs.append(spec)
        agent = Mock(id='private-session', image_id='sha256:fixture')
        agent.start.side_effect = lambda: write_json(spec.control / self.filename, self.report)
        agent.wait.return_value = False
        agent.inspect.return_value = {'Running': False, 'ExitCode': 0}
        yield agent


def plan(sha, path='tests/test_answer.py'):
    return {'strategy': 'focused', 'rationale': 'Require the outcome without prescribing implementation',
            'dependencies': [], 'instructions': 'PYTHONPATH=. python3 tests/test_answer.py',
            'limitations': ['Fixture evidence only'], 'artifacts': [
                {'commit': sha, 'path': path, 'purpose': 'Correct answer regression', 'test_only': True}]}


def report(review):
    return {k: review[k] for k in ('objective_preserved', 'snapshot_reviewed', 'changes')} | {
        'verdict': 'approve', 'reasons': ['Inspected cleanup, test relevance and private evidence boundary']}
