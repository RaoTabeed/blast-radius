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
                affected = self.source_impact(change)
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

    def source_impact(self, change):
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
