"""Shared identities for remote evidence snapshots."""

GRADE_FORMAT = 1


def grade_snapshot(state):
    """The sealed grade is independent of transitions through optional review."""
    payload = state.get('payload', {})
    if payload.get('phase') == 'review' and payload.get('run_id') and payload.get('evaluation_id'):
        return {'id': state['id'], 'state': 'graded', 'grade_format': GRADE_FORMAT, 'payload': payload}
