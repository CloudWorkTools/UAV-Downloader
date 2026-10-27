#!/usr/bin/env python3
"""Portable, deliberately restricted flow inventory and review declaration checker."""
import argparse
import ast
import copy
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile


AST_KINDS = ('python', 'tree-sitter')
ADAPTER_IDS = {
    'python': 'stdlib-python-ast-v2',
    'tree-sitter': 'tree-sitter-language-pack-0.13.0-v1',
    'file': 'raw-bytes-sha256-v1',
}


class Invalid(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def safe_path(path):
    require(isinstance(path, str) and path and '\\' not in path and '\x00' not in path,
            'invalid relative path: ' + repr(path))
    parts = PurePosixPath(path).parts
    require(not path.startswith('/') and all(p not in ('..', '.') for p in parts)
            and str(PurePosixPath(path)) == path, 'unsafe path: ' + path)
    return path


class Tree:
    def __init__(self, root, ref=None, staged=False):
        self.root = root
        self.ref = ref
        self.staged = staged
        self.entries = {}
        require(self.git('rev-parse', '--show-prefix') == b'\n', '--root must name the Git root directory')
        if ref:
            output = self.git('ls-tree', '-rz', '--full-tree', ref)
            for record in output.split(b'\0'):
                if record:
                    header, name = record.split(b'\t', 1)
                    mode, kind, oid = header.split()
                    self.entries[os.fsdecode(name)] = (mode, oid)
        else:
            output = self.git('ls-files', '--stage', '-z')
            for record in output.split(b'\0'):
                if record:
                    header, name = record.split(b'\t', 1)
                    mode, oid, stage = header.split()
                    require(stage == b'0', 'unmerged Git index')
                    self.entries[os.fsdecode(name)] = (mode, oid)
            if not staged:
                for name in self.git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
                    if name:
                        self.entries[os.fsdecode(name)] = (b'100644', None)
                self.entries = {path: entry for path, entry in self.entries.items() if (root / path).exists() or (root / path).is_symlink()}

    def git(self, *args):
        proc = subprocess.run(['git', '-C', str(self.root), *args], capture_output=True)
        require(proc.returncode == 0, 'Git command failed: ' + ' '.join(args) + '\n' + proc.stderr.decode(errors='replace'))
        return proc.stdout

    def read(self, path):
        safe_path(path)
        if self.ref or self.staged:
            require(path in self.entries, 'missing Git input: ' + path)
            mode, oid = self.entries[path]
            require(mode in (b'100644', b'100755'), 'unsupported Git file mode: ' + path)
            return self.git('cat-file', 'blob', oid.decode())
        target = self.root / path
        for part in [target, *target.parents]:
            if part == self.root:
                break
            require(not part.is_symlink(), 'symlink input: ' + path)
        require(target.is_file(), 'missing input: ' + path)
        require(target.resolve().is_relative_to(self.root), 'out-of-root input: ' + path)
        return target.read_bytes()

    def json(self, path):
        return json.loads(self.read(path))


def canonical_python_ast(value):
    if isinstance(value, ast.AST):
        fields = {}
        for name, child in sorted(ast.iter_fields(value)):
            # New optional fields are absent in older runtimes; literal None is a value.
            if child is None and not (isinstance(value, ast.Constant) and name == 'value'):
                continue
            if isinstance(child, list) and not child:
                continue
            fields[name] = canonical_python_ast(child)
        return [type(value).__name__, fields]
    if isinstance(value, list):
        return [canonical_python_ast(child) for child in value]
    if value is None:
        return ['NoneType']
    if value is Ellipsis:
        return ['ellipsis']
    if isinstance(value, bytes):
        return ['bytes', value.hex()]
    if isinstance(value, complex):
        return ['complex', value.real.hex(), value.imag.hex()]
    if isinstance(value, float):
        return ['float', value.hex()]
    if isinstance(value, (str, bool, int)):
        return [type(value).__name__, str(value)]
    raise Invalid('unsupported Python AST scalar: ' + type(value).__name__)


def python_hashes(data, path):
    tree = ast.parse(data, filename=path)
    symbols = {}
    def header(node):
        node = copy.deepcopy(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.body = [ast.Pass()]
        elif isinstance(node, ast.ClassDef):
            node.body = [header(child) if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else child for child in node.body]
        return node
    def visit(body, prefix=''):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                require(name not in symbols, 'duplicate Python symbol: ' + path + ':' + name)
                symbols[name] = digest(canonical_python_ast(node))
            elif isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + '.')
    visit(tree.body)
    context = ast.Module(body=[header(node) for node in tree.body], type_ignores=[])
    return digest(canonical_python_ast(context)), symbols


def source_record(tree, record):
    path = safe_path(record['path'])
    data = tree.read(path)
    result = {'path': path, 'kind': record['kind'], 'md5': hashlib.md5(data, usedforsecurity=False).hexdigest()}
    require(record['kind'] in (*AST_KINDS, 'file'), 'unsupported source kind: ' + path)
    if record['kind'] == 'python':
        result['context_hash'], result['symbols'] = python_hashes(data, path)
    elif record['kind'] == 'tree-sitter':
        language = record.get('language')
        require(isinstance(language, str) and language, 'tree-sitter source requires explicit language: ' + path)
        sys.dont_write_bytecode = True
        try:
            from flow_parsers import ADAPTER_ID, tree_sitter_hashes
        except ImportError as error:
            raise Invalid('optional parser unavailable; copy flow_parsers.py and install requirements-ast.txt: ' + str(error)) from error
        require(ADAPTER_ID == ADAPTER_IDS['tree-sitter'], 'optional parser adapter identity mismatch')
        result['language'] = language
        result['context_hash'], result['symbols'] = tree_sitter_hashes(data, language, path)
    return result


def graph(data, limits, path):
    lines = [line.strip() for line in data.decode('utf-8').splitlines() if line.strip()]
    require(lines, 'empty flow: ' + path)
    mermaid = lines[0] in ('flowchart TD', 'flowchart LR')
    plant = lines[0] == '@startuml' and lines[-1] == '@enduml'
    require(mermaid or plant, 'unsupported diagram header: ' + path)
    nodes, edges = set(), []
    for line in lines[1:] if mermaid else lines[1:-1]:
        if (mermaid and line.startswith('%%')) or (plant and (line.startswith("'") or re.fullmatch(r'(hide (empty description|empty members)|title .+)', line))):
            continue
        match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)\["[^"\n]*"\]|([A-Za-z_][A-Za-z0-9_]*)\{"[^"\n]*"\}', line) if mermaid else re.fullmatch(r'state "[^"\n]*" as ([A-Za-z_][A-Za-z0-9_]*)', line)
        if match:
            node = next(group for group in match.groups() if group is not None)
            require(node not in nodes, 'duplicate flow node: ' + path + ':' + node)
            nodes.add(node)
            continue
        match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*) --> (?:\|[^|\n]+\| )?([A-Za-z_][A-Za-z0-9_]*)', line) if mermaid else re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*) --> ([A-Za-z_][A-Za-z0-9_]*)(?: : .+)?', line)
        # Mermaid's customary labelled arrow has no space after the arrow.
        if mermaid and not match:
            match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*) -->\|[^|\n]+\| ([A-Za-z_][A-Za-z0-9_]*)', line)
        require(match is not None, 'unsupported diagram syntax: ' + path + ': ' + line)
        edges.append(match.groups())
    require(nodes, 'flow has no nodes: ' + path)
    require(len(nodes) <= limits['nodes'], 'flow node limit exceeded: ' + path)
    require(all(a in nodes and b in nodes for a, b in edges), 'undefined edge node: ' + path)
    adjacency = {node: set() for node in nodes}
    for a, b in edges:
        adjacency[a].add(b)
    # Iterative reachability yields SCCs in O(V*(V+E)); bounded by the node limit.
    reach = {}
    for node in nodes:
        seen, pending = set(), [node]
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(adjacency[current] - seen)
        reach[node] = seen
    groups, owner = [], {}
    for node in sorted(nodes):
        if node not in owner:
            group = {other for other in reach[node] if node in reach[other]}
            index = len(groups)
            groups.append(group)
            owner.update({other: index for other in group})
    dag = {i: set() for i in range(len(groups))}
    for a, b in edges:
        if owner[a] != owner[b]:
            dag[owner[a]].add(owner[b])
    weights = {i: int(any(len(adjacency[n]) > 1 for n in group)) for i, group in enumerate(groups)}
    depths = {}
    while len(depths) < len(groups):
        for i in dag:
            if i not in depths and all(child in depths for child in dag[i]):
                depths[i] = weights[i] + max((depths[c] for c in dag[i]), default=0)
    require(max(depths.values(), default=0) <= limits['branch_depth'], 'flow branch depth limit exceeded: ' + path)
    return nodes


def indexed(items, key, label):
    require(isinstance(items, list), label + ' must be a list')
    result = {}
    for item in items:
        require(isinstance(item, dict), 'invalid ' + label)
        identity = item[key]
        require(isinstance(identity, str) and identity and identity not in result, 'duplicate/invalid ' + label + ': ' + str(identity))
        result[identity] = item
    return result


def load(tree, manifest_path, verify=True):
    manifest = tree.json(manifest_path)
    require(manifest.get('version') == 1, 'manifest version must be 1')
    scope = manifest['scope']
    require(isinstance(scope['include'], list) and all(isinstance(p, str) and p for p in scope['include']), 'invalid scope include')
    require(isinstance(scope['exclude'], dict) and all(isinstance(p, str) and p and isinstance(r, str) and r.strip() for p, r in scope['exclude'].items()), 'scope exclusions require reasons')
    sources = indexed(manifest['sources'], 'path', 'sources')
    features = indexed(manifest['features'], 'id', 'features')
    require(features, 'manifest has no features')
    for path, source in sources.items():
        actual = source_record(tree, source)
        if verify:
            for key, value in actual.items():
                require(source.get(key) == value, 'stale ' + key + ': ' + path + '; run snapshot')
    if verify:
        included = {path for path in tree.entries if any(fnmatch.fnmatchcase(path, p) for p in scope['include']) and not any(fnmatch.fnmatchcase(path, p) for p in scope['exclude'])}
        require(included <= sources.keys(), 'unregistered source inputs: ' + ', '.join(sorted(included - sources.keys())))
    limits = {'nodes': 25, 'branch_depth': 8, **manifest.get('limits', {})}
    require(all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in limits.values()), 'invalid complexity limits')
    require(1 <= limits['nodes'] <= 1000, 'supported node budget is 1..1000')
    covered = {path: set() for path in sources}
    for feature in features.values():
        nodes = graph(tree.read(feature['flow']), limits, feature['flow'])
        mapped = set()
        require(isinstance(feature.get('retired', False), bool), 'retired must be boolean')
        if feature.get('retired'):
            require(feature['mappings'] == [] and not feature.get('depends_on'), 'retired feature must have empty mappings and dependencies: ' + feature['id'])
            continue
        require(isinstance(feature['mappings'], list), 'mappings must be a list')
        for mapping in feature['mappings']:
            path = safe_path(mapping['path'])
            require(path in sources, 'mapping references unregistered source: ' + path)
            require(mapping['node'] in nodes, 'mapping references absent node: ' + str(mapping['node']))
            mapped.add(mapping['node'])
            if sources[path]['kind'] in AST_KINDS:
                symbol = mapping.get('symbol')
                require(symbol in sources[path]['symbols'], 'missing AST symbol: ' + path + ':' + str(symbol))
                covered[path].add(symbol)
            else:
                covered[path].add(None)
        require(nodes == mapped, 'unmapped flow nodes: ' + feature['id'] + ': ' + ', '.join(sorted(nodes - mapped)))
        for dependency in feature.get('depends_on', []):
            require(dependency in features and not features[dependency].get('retired'), 'missing/retired feature dependency: ' + str(dependency))
    for path, source in sources.items():
        if source['kind'] in AST_KINDS:
            require(set(source['symbols']) <= covered[path], 'unmapped AST symbols: ' + path + ': ' + ', '.join(sorted(set(source['symbols']) - covered[path])))
            require(covered[path] or not source['symbols'], 'unmapped source: ' + path)
            # Context-only modules still need an explicit mapping; use kind:file.
            require(source['symbols'], 'AST source has no mappable symbols; use kind:file: ' + path)
        else:
            require(covered[path], 'unmapped source: ' + path)
    for path in manifest.get('policy_inputs', []):
        tree.read(path)
    closure(features)
    return manifest, sources, features


def closure(features):
    result = {}
    visiting = set()
    def walk(identity):
        require(identity not in visiting, 'feature dependency cycle: ' + identity)
        if identity not in result:
            visiting.add(identity)
            values = {identity}
            for dependency in features[identity].get('depends_on', []):
                values.update(walk(dependency))
            visiting.remove(identity)
            result[identity] = values
        return result[identity]
    for identity in features:
        walk(identity)
    return result


def policy(manifest, tree):
    return {'scope': manifest['scope'], 'limits': manifest.get('limits', {}), 'hash_adapters': ADAPTER_IDS,
            'policy_inputs': {path: hashlib.sha256(tree.read(path)).hexdigest() for path in manifest.get('policy_inputs', [])}}


def feature_content(identity, tree, sources, features):
    feature = features[identity]
    relevant = []
    for mapping in feature['mappings']:
        source = sources[mapping['path']]
        if source['kind'] in AST_KINDS:
            value = {'context_hash': source['context_hash'], 'symbol_hash': source['symbols'][mapping['symbol']]}
        else:
            value = {'sha256': hashlib.sha256(tree.read(mapping['path'])).hexdigest()}
        adapter = {'kind': source['kind'], 'identity': ADAPTER_IDS[source['kind']]}
        if source['kind'] == 'tree-sitter':
            adapter['language'] = source['language']
        relevant.append({'mapping': mapping, 'adapter': adapter, 'source': value})
    return {'feature': feature, 'flow_sha256': hashlib.sha256(tree.read(feature['flow'])).hexdigest(), 'inputs': relevant}


def fingerprints(tree, loaded, base_oid):
    manifest, sources, features = loaded
    common = policy(manifest, tree)
    contents = {identity: feature_content(identity, tree, sources, features) for identity in features}
    closures = closure(features)
    return {identity: digest({'base': base_oid, 'policy': common, 'features': {key: contents[key] for key in sorted(closures[identity])}}) for identity in features}


def run(args):
    root = Path(args.root).resolve()
    tree = Tree(root, staged=args.staged)
    safe_path(args.manifest)
    if args.command == 'snapshot':
        require(not args.staged, 'snapshot --staged is unsupported; snapshot writes only the worktree and never stages')
        manifest = tree.json(args.manifest)
        require(manifest.get('version') == 1, 'manifest version must be 1')
        indexed(manifest['sources'], 'path', 'sources')
        for source in manifest['sources']:
            source.update(source_record(tree, source))
        # Validate destination too: never follow a manifest symlink on write.
        tree.read(args.manifest)
        destination = root / args.manifest
        mode = destination.stat().st_mode & 0o7777
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=destination.parent,
                                             prefix=destination.name + '.', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(json.dumps(manifest, indent=2, ensure_ascii=True) + '\n')
            temporary.chmod(mode)
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print('snapshot refreshed registered sources (worktree only)')
        return
    oid = tree.git('rev-parse', '--verify', args.base + '^{commit}').decode().strip()
    loaded = load(tree, args.manifest)
    current = fingerprints(tree, loaded, oid)
    if args.command == 'fingerprint':
        require(args.feature in current, 'unknown feature: ' + args.feature)
        print(current[args.feature])
        return
    base_tree = Tree(root, ref=oid)
    if args.manifest not in base_tree.entries:
        affected = set(current)
        print('baseline: base has no manifest; all features require review')
    else:
        previous = load(base_tree, args.manifest)
        removed = previous[2].keys() - loaded[2].keys()
        require(not removed, 'removed features require explicit disposition (unsupported): ' + ', '.join(sorted(removed)))
        old = fingerprints(base_tree, previous, oid)
        affected = {identity for identity in current if current[identity] != old.get(identity)}
    reviews = loaded[0].get('reviews', [])
    require(isinstance(reviews, list), 'reviews must be a list')
    for identity in sorted(affected):
        matching = [review for review in reviews if review.get('feature') == identity and review.get('fingerprint') == current[identity] and review.get('verdict') == 'pass' and isinstance(review.get('reviewer'), str) and review['reviewer'].strip()]
        require(matching, 'missing current review: ' + identity + '; fingerprint ' + current[identity])
        valid = False
        for review in matching:
            evidence = tree.json(review['evidence'])
            if (isinstance(evidence, dict) and all(evidence.get(key) == review[key] for key in ('feature', 'fingerprint', 'reviewer', 'verdict')) and evidence.get('findings') == []):
                valid = True
        require(valid, 'invalid review evidence: ' + identity)
    print('check passed (' + ('index' if args.staged else 'worktree') + '); affected features: ' + ', '.join(sorted(affected)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('snapshot', 'fingerprint', 'check'):
        command = commands.add_parser(name)
        command.add_argument('--root', default='.')
        command.add_argument('--manifest', default='docs/flow-manifest.json')
        command.add_argument('--staged', action='store_true')
        if name != 'snapshot':
            command.add_argument('--base', required=True)
        if name == 'fingerprint':
            command.add_argument('--feature', required=True)
    args = parser.parse_args()
    try:
        run(args)
    except (Invalid, KeyError, TypeError, ValueError, OSError, SyntaxError, RecursionError) as error:
        print('flow checker: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
