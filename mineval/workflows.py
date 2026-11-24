"""Compose local batch workflows with optional VPS transport."""
from mineval.addons.remote import integration
from mineval.addons.batch.workflows import Workflows as BatchWorkflows


class Workflows(BatchWorkflows):
    def __init__(self, store, settings=None, *, context=None, auth_file=None):
        super().__init__(store, settings, context=context, auth_file=auth_file, remote=integration)
