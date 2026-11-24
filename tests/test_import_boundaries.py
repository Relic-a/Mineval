"""Keep benchmark tracks independent and reject module import cycles."""
import ast
from importlib.util import resolve_name
from pathlib import Path
import unittest


def import_graph():
    root = Path(__file__).resolve().parents[1]
    modules = {}
    for path in (root / 'mineval').rglob('*.py'):
        parts = path.relative_to(root).with_suffix('').parts
        name = '.'.join(parts[:-1] if path.name == '__init__.py' else parts)
        modules[name] = path

    graph = {}
    for name, path in modules.items():
        package = name if path.name == '__init__.py' else name.rpartition('.')[0]
        imports = set()
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = (resolve_name('.' * node.level + (node.module or ''), package)
                        if node.level else node.module)
                for alias in node.names:
                    module = f'{base}.{alias.name}'
                    imports.add(module if module in modules else base)
        graph[name] = (imports & modules.keys()) - {name}
    return graph


class ImportBoundaryTests(unittest.TestCase):
    def test_foundation_and_tracks_only_import_allowed_packages(self):
        for source, targets in import_graph().items():
            section = source.split('.')[1:2]
            if not section or section[0] not in ('core', 'arena', 'historical'):
                continue
            allowed = {'core', section[0]}
            for target in targets:
                self.assertTrue(set(target.split('.')[1:2]) & allowed, f'{source} imports {target}')

    def test_imports_have_no_cycles_including_lazy_imports(self):
        graph = import_graph()
        checked = set()

        def check(module, path):
            self.assertNotIn(module, path, ' → '.join([*path, module]))
            if module in checked:
                return
            for dependency in sorted(graph[module]):
                check(dependency, [*path, module])
            checked.add(module)

        for module in graph:
            check(module, [])
