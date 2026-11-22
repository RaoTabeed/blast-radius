import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from impact import ImpactMap, PIN, apply_hunks, parse_patch


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
