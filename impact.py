"""Select tests using the original source and recorded test dependencies."""
import ast
from dataclasses import dataclass
from functools import lru_cache
import gzip
import json
import re

PIN = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
HEADER = re.compile(r'diff --git a/(\S+) b/(\S+)$')
HUNK = re.compile(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
WEEK_OFFSET_READERS = (
    'sqlglot/dialects/dialect.py',
    'sqlglot/generator.py',
    'sqlglot/optimizer/simplify.py',
)
MYSQL_SHOW_PARSERS = {
    'sqlglot/parsers/mysql.py': ('MySQLParser', 'parser.Parser'),
    'sqlglot/parsers/singlestore.py': ('SingleStoreParser', 'MySQLParser'),
}


@dataclass
class FileChange:
    path: str
    hunks: list


def parse_patch(patch):
    """Accept ordinary edits. Return None for structural or unfamiliar patches."""
    lines = patch.split('\n')
    changes = []
    i = 0
    while i < len(lines) and lines[i]:
        header = HEADER.fullmatch(lines[i])
        if not header or header[1] != header[2]:
            return None
        path = header[1]
        i += 1
        if i < len(lines) and lines[i].startswith('index '):
            i += 1
        if lines[i:i + 2] != [f'--- a/{path}', f'+++ b/{path}']:
            return None
        i += 2
        hunks = []
        while i < len(lines) and (match := HUNK.match(lines[i])):
            start, old_count, new_start, new_count = (int(x) if x is not None else 1 for x in match.groups())
            i += 1
            body = []
            old_left, new_left = old_count, new_count
            while old_left or new_left:
                if i == len(lines) or lines[i][:1] not in (' ', '+', '-'):
                    return None
                line = lines[i]
                body.append(line)
                old_left -= line[0] != '+'
                new_left -= line[0] != '-'
                if old_left < 0 or new_left < 0:
                    return None
                i += 1
            hunks.append((start, old_count, new_start, new_count, body))
        if not hunks:
            return None
        changes.append(FileChange(path, hunks))
    if not changes or any(lines[i:]):
        return None
    return changes


def apply_hunks(source, hunks):
    original = source.split('\n')
    result, removed, added_at = [], set(), set()
    cursor = 0
    for start, old_count, new_start, new_count, body in hunks:
        target = start - 1 if old_count else start
        if target < cursor or target > len(original):
            return None
        result.extend(original[cursor:target])
        if len(result) != (new_start - 1 if new_count else new_start):
            return None
        cursor = target
        group_added = group_removed = False
        for line in body:
            kind, content = line[0], line[1:]
            if kind == ' ':
                if group_added and not group_removed:
                    added_at.add(cursor + 1)
                group_added = group_removed = False
            if kind != '+':
                if cursor >= len(original) or original[cursor] != content:
                    return None
                cursor += 1
            if kind == '-':
                group_removed = True
                removed.add(cursor)
            else:
                result.append(content)
                group_added |= kind == '+'
        if group_added and not group_removed:
            added_at.add(cursor + 1)
    return '\n'.join(result + original[cursor:]), removed, added_at


@lru_cache(maxsize=128)
def source_tree(source):
    return ast.parse(source, type_comments=True)


def class_setting_edit(old_tree, new_tree, setting, base, compatible, class_name=None):
    """Match an edit to one class assignment with the rest of the AST intact."""
    def assignment(node):
        return (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == setting)

    for before, after in zip(old_tree.body, new_tree.body):
        if not (isinstance(before, ast.ClassDef) and isinstance(after, ast.ClassDef)
                and not before.keywords and not before.decorator_list
                and len(before.bases) == 1 and ast.unparse(before.bases[0]) == base
                and (class_name is None or before.name == class_name)):
            continue
        for old, new in zip(before.body, after.body):
            if assignment(old) and assignment(new) and compatible(old.value, new.value):
                replacement = new.value
                try:
                    new.value = old.value
                    if ast.dump(old_tree) == ast.dump(new_tree):
                        return True
                finally:
                    new.value = replacement
    return False


def compatible_week_offsets(before, after):
    def offset(node):
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            node = node.operand
        return isinstance(node, ast.Constant) and type(node.value) is int and 0 <= node.value <= 6

    return offset(before) and offset(after)


def compatible_show_parsers(before, after):
    """The trie keys and factories stay fixed. Only the closure options may change."""
    if not (isinstance(before, ast.Dict) and isinstance(after, ast.Dict)
            and len(before.keys) == len(after.keys)):
        return False
    for old, new in zip(before.keys, after.keys):
        if (old is None) != (new is None) or (old is not None and ast.dump(old) != ast.dump(new)):
            return False
    option_types = {'target': (bool, str), 'full': (bool,), 'global_': (bool,)}
    for old, new in zip(before.values, after.values):
        if ast.dump(old) == ast.dump(new):
            continue
        if not all(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                   and call.func.id == '_show_parser' and len(call.args) == 1
                   and isinstance(call.args[0], ast.Constant) and type(call.args[0].value) is str
                   and all(isinstance(kw.value, ast.Constant)
                           and type(kw.value.value) in option_types.get(kw.arg, ())
                           for kw in call.keywords) for call in (old, new)):
            return False
        if old.args[0].value != new.args[0].value:
            return False
    return True


def owners(tree):
    """Find executable scopes and leaf statements that may span multiple lines."""
    scope, statement, predicates = {}, {}, {}

    def visit(node, current=None):
        if isinstance(node, FUNCTIONS):
            # The signature, defaults and decorators execute in the parent scope.
            for child in node.body:
                visit(child, node)
            return
        if isinstance(node, ast.Lambda):
            for line in range(node.body.lineno, node.body.end_lineno + 1):
                scope[line] = node
                statement[line] = node.body
            visit(node.body, node)
            return
        if isinstance(node, ast.BoolOp):
            # An operator change can affect tests that short-circuited before
            # the edited operand. Include callers of the complete expression.
            predicate = (node.lineno, node.end_lineno)
            for line in range(node.lineno, node.end_lineno + 1):
                predicates.setdefault(line, predicate)
        if isinstance(node, ast.stmt) and hasattr(node, 'end_lineno'):
            if current is not None:
                for line in range(node.lineno, node.end_lineno + 1):
                    scope[line] = current
            for line in range(node.lineno, node.end_lineno + 1):
                statement[line] = node
        for child in ast.iter_child_nodes(node):
            visit(child, current)

    visit(tree)
    return scope, statement, predicates


class ImpactMap:
    def __init__(self, path, tests):
        with gzip.open(path, 'rt', encoding='utf-8') as handle:
            data = json.load(handle)
        if data['revision'] != PIN:
            raise ValueError('Impact map revision does not match the challenge pin')
        self.files = data['files']
        self.fixtures = data['fixtures']
        self.ids = data['ids']
        missing = set(data['unmapped']) | ({test['id'] for test in tests} - set(self.ids))
        self.uncertain = {test['number'] for test in tests if test['id'] in missing}
        self.numbers = {test['id']: test['number'] for test in tests}

    def select(self, patch):
        changes = parse_patch(patch)
        if changes is None:
            return None
        mask = 0
        for change in changes:
            # Data files loaded during class setup or native I/O can be shared
            # without a per-test Python open. Use the map only for SQL fixtures.
            if change.path.endswith('.sql') and change.path in self.fixtures:
                affected = int(self.fixtures[change.path], 16) or None
            elif change.path in self.files:
                affected = self.source_impact(change, single_file=len(changes) == 1)
            else:
                return None
            if affected is None:
                return None
            mask |= affected
        numbers = set(self.uncertain)
        while mask:
            bit = mask & -mask
            number = self.numbers.get(self.ids[bit.bit_length() - 1])
            if number is not None:
                numbers.add(number)
            mask ^= bit
        return numbers

    def week_offset_impact(self):
        # At the pinned revision, Dialect's class initializer does not use
        # WEEK_OFFSET. Only these runtime attribute reads consume its value.
        # Include read sites for every receiver, including other dialects.
        affected = 0
        for path in WEEK_OFFSET_READERS:
            if path not in self.files:
                return None
            entry = self.files[path]
            collection = set(entry['collection'])
            readers = [node for node in ast.walk(source_tree(entry['source']))
                       if isinstance(node, ast.Attribute) and node.attr == 'WEEK_OFFSET']
            if not readers:
                return None
            for node in readers:
                line = node.lineno
                mask = int(entry['lines'].get(str(line), '0'), 16)
                if not mask or line in collection:
                    return None
                affected |= mask
        return affected

    def mysql_show_impact(self):
        # The factory only captures constants. SHOW_TRIE is derived from the
        # dictionary's keys, and dispatch observes values through the closure.
        entry = self.files.get('sqlglot/parsers/mysql.py')
        if entry is None:
            return None
        tree = source_tree(entry['source'])
        for node in tree.body:
            if (isinstance(node, ast.FunctionDef) and node.name == '_show_parser'
                    and len(node.body) == 2):
                closure, result = node.body
                if (not isinstance(closure, ast.FunctionDef) or not isinstance(result, ast.Return)
                        or len(closure.body) != 1 or not isinstance(closure.body[0], ast.Return)):
                    return None
                line = closure.body[0].lineno
                if line not in entry['collection']:
                    return int(entry['lines'].get(str(line), '0'), 16) or None
        return None

    def class_setting_impact(self, path, old_tree, new_tree):
        if (path.startswith('sqlglot/dialects/') and path != 'sqlglot/dialects/dialect.py'
                and class_setting_edit(old_tree, new_tree, 'WEEK_OFFSET', 'Dialect',
                                       compatible_week_offsets)):
            return self.week_offset_impact()
        if path in MYSQL_SHOW_PARSERS:
            name, base = MYSQL_SHOW_PARSERS[path]
            if class_setting_edit(old_tree, new_tree, 'SHOW_PARSERS', base,
                                  compatible_show_parsers, class_name=name):
                return self.mysql_show_impact()
        return None

    def source_impact(self, change, *, single_file=False):
        entry = self.files[change.path]
        source = entry['source']
        applied = apply_hunks(source, change.hunks)
        if applied is None:
            return None
        changed, removed, added_at = applied
        try:
            old_tree = source_tree(source)
            new_tree = ast.parse(changed, type_comments=True)
            compile(new_tree, change.path, 'exec')
        except (SyntaxError, ValueError):
            return None
        readers = int(entry.get('readers', '0'), 16)
        if ast.dump(old_tree) == ast.dump(new_tree):
            return readers
        scope, statements, predicates = owners(old_tree)
        coverage = {int(line): int(mask, 16) for line, mask in entry['lines'].items()}
        collection = set(entry['collection'])
        affected = 0
        for line in removed:
            function = scope.get(line)
            if function is None or line in collection:
                if single_file:
                    dependencies = self.class_setting_impact(change.path, old_tree, new_tree)
                    if dependencies is not None:
                        for definition_line in removed:
                            dependencies |= coverage.get(definition_line, 0)
                        return dependencies | readers
                return None
            stmt = statements[line]
            direct = coverage.get(line, 0)
            if line in predicates:
                start, end = predicates[line]
                if any(predicate_line in collection for predicate_line in range(start, end + 1)):
                    return None
                for predicate_line in range(start, end + 1):
                    direct |= coverage.get(predicate_line, 0)
            elif not direct and not any(isinstance(node, ast.stmt) for node in ast.iter_child_nodes(stmt)):
                for stmt_line in range(stmt.lineno, stmt.end_lineno + 1):
                    direct |= coverage.get(stmt_line, 0)
            if not direct:
                for body_line in range(function.lineno + 1, function.end_lineno + 1):
                    direct |= coverage.get(body_line, 0)
            affected |= direct
        for line in added_at:
            function = scope.get(line)
            if function is None or function is not scope.get(line - 1):
                return None
            for body_line in range(function.lineno + 1, function.end_lineno + 1):
                affected |= coverage.get(body_line, 0)
        return (affected | readers) if affected else None
