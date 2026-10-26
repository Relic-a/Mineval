"""Persistent rootless Arena demonstration. No real-model calls."""
import argparse
import json
from pathlib import Path

from mineval.arena import Arena
from mineval.arena_runtime import Runner, RuntimeManager
from mineval.artifacts import digest


def demonstrate(directory, context, image):
    directory = Path(directory).resolve()
    if directory.exists():
        raise ValueError('Choose a new demo directory')
    arena = Arena(directory)
    task = arena.create({'title': 'A quiet place to read',
        'prompt': 'Build a thoughtful reading app with saved articles, warm typography and a calm interface.',
        'private': False})
    configs = [dict(harness='fixture', model='fixture-studio', effort=e) for e in ('medium','high')]
    runs = arena.enqueue(task['id'], configs)
    runner = Runner(arena, image, context)
    for run in runs:
        result = runner.generate(run['id'])
        if result['status'] != 'ready': raise RuntimeError(result['error'])
    pair = arena.comparison(task['id'])
    arena.judge(task['id'], pair['left'], pair['right'], 'tie', 'Synthetic fixture judgment; not a model-quality result.')
    before = [digest(arena.artifact(r['id'])) for r in runs]
    manager = RuntimeManager(arena, runner.sandbox)
    try:
        manager.open(runs[0]['id'])
    finally:
        manager.close()
    reopened = Arena(directory)
    assert before == [digest(reopened.artifact(r['id'])) for r in runs]
    assert len(reopened.records('judgments', task['id'])) == 1
    summary = dict(benchmark=task['id'], generations=[r['id'] for r in runs],
        store=str(directory), artifacts_preserved=True, real_model_calls=0)
    (directory/'demo-summary.json').write_text(json.dumps(summary, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--context', default='mineval-rootless')
    parser.add_argument('--image', required=True, help='preloaded rootless image with Python3')
    args = parser.parse_args()
    print(json.dumps(demonstrate(args.directory, args.context, args.image), indent=2))
    print(f'Open with: python -m mineval --context {args.context} --data {args.directory} arena serve --image {args.image}')


if __name__ == '__main__':
    main()
