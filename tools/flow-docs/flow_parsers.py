"""Optional, local Tree-sitter adapters. Unknown syntax remains in module context."""
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import re

ADAPTER_ID = 'tree-sitter-language-pack-0.13.0-v1'
LANGUAGES = frozenset(('javascript', 'typescript', 'tsx', 'go', 'rust', 'java',
                       'csharp', 'c', 'cpp', 'gdscript', 'sql', 'prisma', 'json'))
COMMENT_TYPES = frozenset(('comment', 'line_comment', 'block_comment', 'marginalia'))
# These comments affect tools/builds; ordinary prose comments do not affect hashes.
DIRECTIVE = re.compile(rb'go:|@|eslint|prettier|istanbul|c8\b|sourceMappingURL|sourceURL|^///|^//!|^/\*[!+]|^//\s*line\s|^/\*line\s|#(?:if|else|endif|pragma|region|endregion)|coding[:=]|noinspection|deno-|biome-|jshint|flowlint', re.I)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def tree_sitter_hashes(data: bytes, language: str, path: str):
    """Return context and independently tracked definitions; never execute source.

    Overloads and duplicate qualified names deliberately require whole-file tracking.
    JSON tracks root object keys; nested values belong to that root key's hash.
    """
    if language not in LANGUAGES:
        raise ValueError(f'unsupported Tree-sitter language {language!r}: {path}; use kind:file')
    try:
        from tree_sitter_language_pack import get_parser
        installed = version('tree-sitter-language-pack')
    except (ImportError, PackageNotFoundError) as exc:
        raise ValueError('Tree-sitter requires optional tree-sitter-language-pack==0.13.0; '
                         'install it with pip or use kind:file') from exc
    if installed != '0.13.0':
        raise ValueError(f'Tree-sitter requires tree-sitter-language-pack==0.13.0, found {installed}; '
                         'install the pinned version or use kind:file')
    try:
        root = get_parser(language).parse(data).root_node
    except Exception as exc:
        raise ValueError(f'cannot load Tree-sitter language {language!r}: {path}; use kind:file: {exc}') from exc
    if root.has_error:
        raise ValueError(f'Tree-sitter parse error in {path} ({language}); fix syntax or use kind:file')

    symbols = {}
    masks = {}

    def raw(node):
        return data[node.start_byte:node.end_byte].decode('utf-8', errors='strict') if node else ''

    def field(node, name):
        return node.child_by_field_name(name)

    def normalized(node, replacements=None):
        replacement = (replacements or {}).get(node.id)
        if replacement is not None:
            return replacement
        if node.type in COMMENT_TYPES:
            text = data[node.start_byte:node.end_byte]
            return [node.type, raw(node)] if DIRECTIVE.search(text) else None
        if not node.children:
            # Literal leaves (including template characters and JSX text) stay exact.
            return [node.type, raw(node)]
        children = [normalized(child, replacements) for child in node.children]
        return [node.type, [child for child in children if child is not None]]

    def compact(node):
        if node is None:
            return ''
        if not node.children:
            return raw(node)
        return ''.join(compact(child) for child in node.children if child.type not in COMMENT_TYPES)

    def register(name, node, body=None, schema=False):
        if not name:
            return
        if name in symbols:
            raise ValueError(f'duplicate Tree-sitter symbol {path}:{name}; overloads or ambiguous '
                             'names require kind:file')
        symbols[name] = _digest(normalized(node))
        if schema:
            masks[node.id] = ['schema', name]
        elif body is not None:
            masks[body.id] = ['body']

    def declarator_name(node):
        if node is None:
            return ''
        if node.type in ('identifier', 'field_identifier', 'qualified_identifier',
                         'operator_name', 'destructor_name'):
            return compact(node)
        child = field(node, 'declarator')
        if child is None and node.type == 'parenthesized_declarator':
            child = next(iter(node.named_children), None)
        return declarator_name(child) if child else ''

    def visit(node, scope=()):
        kind = node.type
        body = field(node, 'body')
        name = raw(field(node, 'name'))
        separator = '::' if language in ('rust', 'cpp') else '.'
        qualified = lambda value: separator.join((*scope, value))
        if language in ('javascript', 'typescript', 'tsx'):
            if kind in ('function_declaration', 'generator_function_declaration'):
                register(qualified(name), node, body)
                return
            if kind == 'variable_declarator':
                value = field(node, 'value')
                if value and value.type in ('arrow_function', 'function_expression', 'generator_function'):
                    target = field(node, 'name')
                    if target.type == 'identifier':
                        register(qualified(raw(target)), node, field(value, 'body'))
                        return
            if kind == 'method_definition' and scope:
                register(qualified(compact(field(node, 'name'))), node, body)
                return
            if kind in ('class_declaration', 'class'):
                scope = (*scope, name) if name else scope
        elif language == 'go':
            if kind in ('function_declaration', 'method_declaration'):
                receiver = field(node, 'receiver')
                if receiver:
                    parameter = next((child for child in receiver.named_children
                                      if child.type == 'parameter_declaration'), None)
                    receiver_type = field(parameter, 'type') if parameter else None
                    name = compact(receiver_type) + '.' + name
                register(name, node, body)
                return
        elif language == 'rust':
            if kind in ('function_item', 'function_signature_item'):
                register(qualified(name), node, body)
                return
            if kind == 'impl_item':
                target, trait = compact(field(node, 'type')), compact(field(node, 'trait'))
                scope = (*scope, (trait + ' for ' if trait else '') + target)
            elif kind in ('trait_item', 'mod_item'):
                scope = (*scope, name)
        elif language in ('java', 'csharp'):
            if kind in ('method_declaration', 'constructor_declaration', 'local_function_statement'):
                register(qualified(name), node, body)
                return
            if kind in ('class_declaration', 'interface_declaration', 'struct_declaration',
                        'record_declaration', 'enum_declaration', 'namespace_declaration'):
                scope = (*scope, name)
        elif language in ('c', 'cpp'):
            if kind == 'function_definition':
                register(qualified(declarator_name(field(node, 'declarator'))), node, body)
                return
            if kind in ('namespace_definition', 'class_specifier', 'struct_specifier') and name:
                scope = (*scope, name)
        elif language == 'gdscript':
            if kind == 'function_definition':
                register(qualified(name), node, body)
                return
            if kind == 'class_definition' and name:
                scope = (*scope, name)
        elif language == 'sql' and kind == 'create_table':
            reference = next((child for child in node.named_children
                              if child.type == 'object_reference'), None)
            if reference:
                register('table.' + compact(reference), node, schema=True)
                return
        elif language == 'prisma' and kind == 'model_block':
            identifier = next((child for child in node.named_children if child.type == 'identifier'), None)
            if identifier:
                register('model.' + raw(identifier), node, schema=True)
                return
        elif language == 'json' and kind == 'document':
            obj = next(iter(node.named_children), None)
            if obj and obj.type == 'object':
                for pair in obj.named_children:
                    if pair.type == 'pair':
                        key = json.loads(raw(field(pair, 'key')))
                        pointer = '/' + key.replace('~', '~0').replace('/', '~1')
                        register(pointer, pair, schema=True)
            return
        for child in node.named_children:
            visit(child, scope)

    try:
        visit(root)
        context = _digest(normalized(root, masks))
    except UnicodeDecodeError as exc:
        raise ValueError(f'Tree-sitter source must be UTF-8: {path}; use kind:file') from exc
    if not symbols:
        raise ValueError(f'no supported Tree-sitter symbols in {path} ({language}); use kind:file')
    return context, symbols
