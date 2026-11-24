"""Default Historical store with verified dependency caching enabled."""
from mineval.addons.cache.dependency_cache import cache_task
from mineval.addons.grading.http_grading import execute as grade_http
from mineval.historical.preparation import Store as HistoricalStore
from mineval.historical.preparation import commit, export_git, identifier


class Store(HistoricalStore):
    def __init__(self, root, *, attach_cache=cache_task, graders=None):
        if graders is None:
            graders = {'isolated-http-v1': grade_http}
        super().__init__(root, attach_cache=attach_cache, graders=graders)
