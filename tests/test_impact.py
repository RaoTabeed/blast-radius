import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from impact import ImpactMap, PIN, WEEK_OFFSET_READERS, apply_hunks, parse_patch


def ordinary_diff(*body, path='sample.py'):
    return '\n'.join([f'diff --git a/{path} b/{path}', f'--- a/{path}',
                      f'+++ b/{path}', *body, ''])


class PatchTests(unittest.TestCase):
    def test_replacement_and_insert_use_original_coordinates(self):
        change, = parse_patch(ordinary_diff('@@ -1,3 +1,4 @@', ' first',
                                            '+inserted', ' second', '-third', '+last'))
        self.assertEqual(apply_hunks('first\nsecond\nthird\n', change.hunks),
                         ('first\ninserted\nsecond\nlast\n', {3}, {2}))

    def test_insert_before_first_line(self):
        change, = parse_patch(ordinary_diff('@@ -0,0 +1 @@', '+first'))
        self.assertEqual(apply_hunks('second\n', change.hunks), ('first\nsecond\n', set(), {1}))

    def test_context_mismatch_falls_back(self):
        change, = parse_patch(ordinary_diff('@@ -1 +1 @@', '-something', '+replacement'))
        self.assertIsNone(apply_hunks('unexpected\n', change.hunks))

    def test_structural_and_truncated_patches_fall_back(self):
        self.assertIsNone(parse_patch('diff --git a/old.py b/new.py\n'))
        self.assertIsNone(parse_patch(ordinary_diff('@@ -1 +1 @@', '-something')))


class ImpactTests(unittest.TestCase):
    def setUp(self):
        data = {'revision': PIN, 'ids': ['test_body', 'test_source'], 'unmapped': [],
                'files': {'sample.py': {'source': 'def f():\n    return 1\n',
                                       'lines': {'2': '1'}, 'collection': [1], 'readers': '2'}},
                'fixtures': {'tests/fixtures/query.sql': '1'}}
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'map.gz'
            path.write_bytes(gzip.compress(json.dumps(data).encode()))
            self.impact = ImpactMap(path, [{'id': 'test_source', 'number': 0},
                                           {'id': 'test_new', 'number': 1},
                                           {'id': 'test_body', 'number': 2}])

    def test_comment_preserves_source_readers_and_unmapped_tests(self):
        diff = ordinary_diff('@@ -2 +2 @@', '-    return 1', '+    return 1  # note')
        self.assertEqual(self.impact.select(diff), {0, 1})

    def test_body_edit_uses_coverage_and_current_test_numbers(self):
        diff = ordinary_diff('@@ -2 +2 @@', '-    return 1', '+    return 2')
        self.assertEqual(self.impact.select(diff), {0, 1, 2})

    def test_equal_literals_with_different_types_are_an_edit(self):
        diff = ordinary_diff('@@ -2 +2 @@', '-    return 1', '+    return 1.0')
        self.assertEqual(self.impact.select(diff), {0, 1, 2})

    def test_signature_edit_falls_back(self):
        diff = ordinary_diff('@@ -1 +1 @@', '-def f():', '+def f(arg):')
        self.assertIsNone(self.impact.select(diff))

    def test_short_circuit_edit_includes_other_operands(self):
        self.impact.files['sample.py'] = {
            'source': 'def f(a, b):\n    return (\n        a\n        and b\n    )\n',
            'lines': {'3': '2', '4': '1'}, 'collection': [], 'readers': '0',
        }
        diff = ordinary_diff('@@ -4 +4 @@', '-        and b', '+        or b')
        self.assertEqual(self.impact.select(diff), {0, 1, 2})

    def test_week_offset_value_edit_includes_readers_and_definition_coverage(self):
        path = 'sqlglot/dialects/example.py'
        self.impact.files[path] = {
            'source': 'class Example(Dialect):\n    WEEK_OFFSET = -1\n',
            'lines': {'2': '2'}, 'collection': [], 'readers': '0',
        }
        for reader in WEEK_OFFSET_READERS:
            self.impact.files[reader] = {
                'source': 'return dialect.WEEK_OFFSET\n',
                'lines': {'1': '1'}, 'collection': [],
            }
        diff = ordinary_diff('@@ -2 +2 @@', '-    WEEK_OFFSET = -1', '+    WEEK_OFFSET = -2', path=path)
        self.assertEqual(self.impact.select(diff), {0, 1, 2})
        self.impact.files[WEEK_OFFSET_READERS[0]]['collection'] = [1]
        self.assertIsNone(self.impact.select(diff))

    def test_week_offset_expression_edit_falls_back(self):
        path = 'sqlglot/dialects/example.py'
        self.impact.files[path] = {
            'source': 'class Example(Dialect):\n    WEEK_OFFSET = -1\n',
            'lines': {'2': '2'}, 'collection': [], 'readers': '0',
        }
        diff = ordinary_diff('@@ -2 +2 @@', '-    WEEK_OFFSET = -1', '+    WEEK_OFFSET = get_offset()', path=path)
        self.assertIsNone(self.impact.select(diff))

    def test_show_parser_options_use_closure_coverage(self):
        path = 'sqlglot/parsers/singlestore.py'
        self.impact.files[path] = {
            'source': 'class SingleStoreParser(MySQLParser):\n'
                      '    SHOW_PARSERS = {"A": _show_parser("A", target=True)}\n',
            'lines': {'2': '2'}, 'collection': [2],
        }
        self.impact.files['sqlglot/parsers/mysql.py'] = {
            'source': 'def _show_parser(*args, **kwargs):\n'
                      '    def parse(self):\n'
                      '        return self._parse_show_mysql(*args, **kwargs)\n'
                      '    return parse\n',
            'lines': {'3': '1'}, 'collection': [],
        }
        diff = ordinary_diff('@@ -2 +2 @@', '-    SHOW_PARSERS = {"A": _show_parser("A", target=True)}',
                             '+    SHOW_PARSERS = {"A": _show_parser("A", target=False)}', path=path)
        self.assertEqual(self.impact.select(diff), {0, 1, 2})
        self.assertIsNone(self.impact.select(diff.replace('+    SHOW_PARSERS = {"A"',
                                                          '+    SHOW_PARSERS = {"B"')))

    def test_numeric_escape_edit_keeps_decoder_coverage_and_rejects_key_changes(self):
        path = 'sqlglot/dialects/example.py'
        self.impact.files[path] = {
            'source': 'class Example(Dialect):\n    class Tokenizer(tokens.Tokenizer):\n'
                      '        NUMERIC_ESCAPES = {"u": (16, 4, 4, 0xFFFF)}\n',
            'lines': {'3': '2'}, 'collection': [3],
        }
        self.impact.files['sqlglot/tokenizer_core.py'] = {
            'source': 'def decode(self):\n    return self._scan_numeric_escape()\n',
            'lines': {'2': '1'}, 'collection': [],
        }
        diff = ordinary_diff('@@ -3 +3 @@', '-        NUMERIC_ESCAPES = {"u": (16, 4, 4, 0xFFFF)}',
                             '+        NUMERIC_ESCAPES = {"u": (8, 4, 4, 0xFFFF)}', path=path)
        self.assertEqual(self.impact.select(diff), {0, 1, 2})
        self.assertIsNone(self.impact.select(diff.replace('+        NUMERIC_ESCAPES = {"u"',
                                                          '+        NUMERIC_ESCAPES = {"U"')))
