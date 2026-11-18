"""Pinned Git sources and lazily reconstructed private evidence."""
from contextlib import contextmanager
from pathlib import Path
import tempfile

from .artifacts import MAX_BYTES, MAX_FILES, digest, inventory, read_json, relative, write_json
from .history import create_pack, git, import_pack, pack_commits, restore


class Intake:
    def __init__(self, repo, private):
        self.repo = repo
        self.private = private
        self.submodules = {}
        self.base_modules = {}
        self.root = None

    def export(self, sha, tree, *, base=False):
        from .preparation import export_git
        self.root = tree
        self.base = base
        export_git(self.repo, sha, tree, submodule=self.module)

    def module(self, repo, parent, path, sha, target, budget):
        from .preparation import export_git
        full = (target / path).relative_to(self.root).as_posix()
        key = full + '@' + sha
        if len(Path(full).parts) > 64 or len(self.submodules) >= 1000:
            raise ValueError(f'Submodule limit (64 path components, 1000 commits) exceeded at {full}')
        try:
            local = repo / path
            candidates = []
            if local.resolve() == local and (local / '.git').exists():
                if git(local, 'rev-parse', '--show-toplevel').decode().strip() == str(local):
                    candidates.append(local)
            # Deinitialized modules may still have all their objects locally.
            try:
                records = git(repo, 'config', '--null', '--blob', parent + ':.gitmodules',
                              '--get-regexp', r'^submodule\..*\.path$')
            except ValueError:
                records = b''
            common = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip())
            for record in records.split(b'\0'):
                if record and record.split(b'\n', 1)[1].decode() == path.as_posix():
                    name = record.split(b'\n', 1)[0].decode()[10:-5]
                    location = common / 'modules' / relative(name)
                    if location.resolve() == location and (location / 'objects').is_dir():
                        candidates.append(location)
            for local in candidates:
                try:
                    if git(local, 'rev-parse', '--verify', sha + '^{commit}').decode().strip() == sha:
                        break
                except ValueError:
                    continue
            else:
                raise ValueError('Pinned commit is unavailable in local submodule objects')
            if key not in self.submodules:
                pack_path = f'submodule-packs/{len(self.submodules):04d}.pack'
                (self.private / pack_path).parent.mkdir(exist_ok=True)
                create_pack(local, sha, self.private / pack_path)
                self.submodules[key] = pack_path
                inventory(self.private)
            if self.base:
                self.base_modules[full] = {'commit': sha, 'pack': self.submodules[key]}
            export_git(local, sha, target / path, submodule=self.module, validate=False, budget=budget)
        except (OSError, ValueError) as exc:
            raise ValueError(f'Cannot preserve submodule {full} at {sha}: {exc}') from exc

    def finish(self, futures):
        pack_commits(self.repo, futures, self.private / 'future.pack')
        write_json(self.private / 'git-sources.json', {'submodules': self.submodules,
                                                        'base_modules': self.base_modules})


@contextmanager
def future_tree(task, sha):
    private = task / 'private'
    descriptor = private / 'git-sources.json'
    expected = read_json(task / 'source-hashes.json')
    if not descriptor.exists():
        tree = private / 'future' / sha
        prefix = f'future/{sha}/'
        if inventory(tree) != {p[len(prefix):]: v for p, v in expected.items() if p.startswith(prefix)}:
            raise ValueError('Stored future state was modified')
        yield tree
        return
    from .preparation import export_git
    for name in ('git-sources.json', 'future.pack'):
        if digest(private / name) != expected[name]['sha256']:
            raise ValueError(f'Stored source evidence was modified: {name}')
    modules = read_json(descriptor)['submodules']
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        root = temp / 'tree'
        import_pack(private / 'future.pack', temp / 'git', sha)

        def module(repo, parent, path, commit, target, budget):
            full = (target / path).relative_to(root).as_posix()
            name = modules[full + '@' + commit]
            pack = private / relative(name)
            if digest(pack) != expected[name]['sha256']:
                raise ValueError(f'Modified submodule source: {full}')
            with tempfile.TemporaryDirectory(dir=temp) as source:
                import_pack(pack, source, commit)
                export_git(source, commit, target / path, submodule=module, validate=False, budget=budget)

        export_git(temp / 'git', sha, root, submodule=module)
        yield root


def copy_futures(task, target):
    from .artifacts import copy_tree
    count = size = 0
    for sha in read_json(task / 'metadata.json')['future_commits']:
        with future_tree(task, sha) as tree:
            entries = inventory(tree)
            count += len(entries)
            size += sum(len(item['symlink'].encode('utf-8', 'surrogateescape')) if 'symlink' in item
                        else (tree / path).stat().st_size for path, item in entries.items())
            if count > MAX_FILES or size > MAX_BYTES:
                raise ValueError(f'Private review exceeds {MAX_FILES} entries or {MAX_BYTES} bytes at future/{sha}')
            copy_tree(tree, target / sha, expected_inventory=entries)


def attach_modules(workspace, directory, modules):
    for path, record in modules.items():
        current = workspace
        for part in relative(path).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f'Submodule path is a symbolic link: {path}')
        if current.is_dir():
            restore(directory / relative(record['pack']), record['commit'], current)

def attach_reference_history(task, sha, workspace):
    """Attach the reviewed commit and its recorded modules to a private reference."""
    private = task / 'private'
    expected = read_json(task / 'source-hashes.json')
    for name in ('git-sources.json', 'future.pack'):
        if digest(private / name) != expected[name]['sha256']:
            raise ValueError(f'Stored source evidence was modified: {name}')
    descriptor = read_json(private / 'git-sources.json')
    import_pack(private / 'future.pack', workspace, sha)
    git(workspace, 'update-ref', 'refs/heads/reference', sha)
    git(workspace, 'symbolic-ref', 'HEAD', 'refs/heads/reference')
    git(workspace, 'read-tree', sha)

    def submodules(path, parent):
        modules = {}
        for entry in git(workspace / path, 'ls-tree', '-rz', '--full-tree', parent).split(b'\0'):
            if not entry.startswith(b'160000 commit '):
                continue
            header, name = entry.split(b'\t', 1)
            commit = header.split()[2].decode()
            full = (path / relative(name.decode())).as_posix()
            pack = descriptor['submodules'][full + '@' + commit]
            if digest(private / pack) != expected[pack]['sha256']:
                raise ValueError(f'Modified submodule source: {full}')
            modules[full] = {'pack': pack, 'commit': commit}
        attach_modules(workspace, private, modules)
        for full, item in modules.items():
            submodules(Path(full), item['commit'])

    submodules(Path('.'), sha)
