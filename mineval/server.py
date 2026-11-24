"""Compose the Arena web UI with read-only historical routes."""
from pathlib import Path
from urllib.parse import parse_qs, unquote

from .arena import server as arena_server
from mineval.addons.batch import dashboard
from mineval.historical import observation
from mineval.preparation import Store


def historical_pages(root):
    def render(prefix, path, query):
        return historical_view(Store(root), prefix).render(unquote(path), parse_qs(query)).encode()
    return render


def make_arena_server(app, port=0):
    return arena_server.make_server(app, port, historical=historical_pages(app.arena.root.parent))


def serve_arena(root, image, **options):
    return arena_server.serve(root, image, historical=historical_pages(Path(root)), **options)


def historical_view(store, prefix='', workflows=None, csrf=''):
    return observation.Pages(store, prefix, workflows, csrf, views=dashboard)


def make_historical_server(store, port=0, workflows=None):
    return observation.make_server(store, port, workflows, views=dashboard)


def serve_historical(store, port=0, workflows=None):
    return observation.serve(store, port, workflows, views=dashboard)
