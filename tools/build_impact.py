"""Record test dependencies on the unchanged, pinned SQLGlot checkout."""
import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import coverage
import pytest

from trace_worker import traced_call

PIN = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'


class Recorder:
    def __init__(self, root, cov):
        self.root = root
        self.cov = cov
        self.current = ''
        self.processed = set()
        self.fixture_readers = defaultdict(set)
        self.source_readers = defaultdict(set)

    def record_open(self, event, args):
        if event != 'open' or not isinstance(args[0], str):
            return
        path = args[0].replace('\\', '/')
        fixture_candidate = 'fixtures/' in path
        source_candidate = self.current and path.endswith('.py') and 'sqlglot/' in path
        if not (fixture_candidate or source_candidate):
            return
        try:
            relative = Path(path).absolute().relative_to(self.root).as_posix()
        except ValueError:
            return
        if relative.startswith('tests/fixtures/'):
            self.fixture_readers[relative].add(self.current)
        elif source_candidate and relative.startswith('sqlglot/'):
            self.source_readers[relative].add(self.current)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        self.current = item.nodeid
        self.cov.switch_context(self.current)
        try:
            return (yield)
        finally:
            self.processed.add(self.current)
            self.current = ''
            self.cov.switch_context('')


def build_map(root, manifest, output, targets, merge_from):
    revision = subprocess.check_output(
        ['git', '-c', f'safe.directory={root.as_posix()}', 'rev-parse', 'HEAD'],
        cwd=root, text=True,
    ).strip()
    if revision != PIN:
        raise SystemExit(f'Expected SQLGlot revision {PIN}, found {revision}')
    changes = subprocess.check_output(['git', '-c', f'safe.directory={root.as_posix()}',
                                       'status', '--porcelain', '--untracked-files=no'], cwd=root)
    if changes:
        raise SystemExit('SQLGlot checkout has tracked changes')
    ids = [test['id'] for test in json.loads(manifest.read_text('utf-8'))['tests']]
    positions = {nodeid: 1 << i for i, nodeid in enumerate(ids)}
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ['SKIP_INTEGRATION'] = '0'
    cov = coverage.Coverage(data_file=None, source=[str(root / 'sqlglot')])
    recorder = Recorder(root, cov)
    pool_tests = set()
    sys.addaudithook(recorder.record_open)
    with TemporaryDirectory(prefix='blast-coverage-', dir=output.parent) as tmp:
        filename = str(Path(tmp) / '.coverage-workers')
        original_submit = ProcessPoolExecutor.submit

        def submit(pool, fn, /, *args, **kwargs):
            if not recorder.current:
                return original_submit(pool, fn, *args, **kwargs)
            pool_tests.add(recorder.current)
            return original_submit(pool, traced_call, filename, str(root / 'sqlglot'),
                                   recorder.current, fn, args, kwargs)

        with cov.collect(), patch.object(ProcessPoolExecutor, 'submit', submit):
            result = pytest.main(['-q', '--color=no', '-p', 'no:cacheprovider', *targets], plugins=[recorder])
        worker_cov = coverage.Coverage(data_file=filename)
        worker_cov.combine()
        workers = coverage.CoverageData(no_disk=True)
        workers.update(worker_cov.get_data())
    if result not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        raise SystemExit(f'Pytest did not complete collection and execution: {result}')

    def pack(contexts):
        return format(sum(positions.get(name, 0) for name in set(contexts)), 'x')

    data = cov.get_data()
    files = {}
    for name in sorted(data.measured_files() | workers.measured_files()):
        path = Path(name)
        relative = path.relative_to(root).as_posix()
        contexts = data.contexts_by_lineno(name)
        collection = [line for line, names in contexts.items() if '' in names]
        worker_contexts = workers.contexts_by_lineno(name)
        for line, names in worker_contexts.items():
            contexts.setdefault(line, []).extend(names)
            if '' in names:
                # ProcessPoolExecutor also runs code between calls, such as
                # unpickling inputs and pickling return values on the worker.
                # The exact test is unknown at that point. Keep every submitter.
                contexts[line].extend(pool_tests)
        if not contexts:
            continue
        files[relative] = {
            'source': path.read_text('utf-8'),
            'lines': {line: pack(names) for line, names in contexts.items()},
            'collection': collection,
            'readers': pack(recorder.source_readers.get(relative, ())),
        }
    fixtures = {path: pack(readers) for path, readers in sorted(recorder.fixture_readers.items())
                if '' not in readers}
    skipped_or_absent = sorted(set(ids) - recorder.processed)
    print(f'Recorded {len(ids) - len(skipped_or_absent)}/{len(ids)} tests, '
          f'{len(files)} source files, {len(fixtures)} fixtures. Pytest exit: {result}.')
    if skipped_or_absent:
        print('Unmapped IDs:', *skipped_or_absent[:20], sep='\n')
    payload = {'revision': PIN, 'ids': ids, 'files': files, 'fixtures': fixtures,
               'unmapped': skipped_or_absent}
    if merge_from:
        with gzip.open(merge_from, 'rt', encoding='utf-8') as handle:
            earlier = json.load(handle)
        complete = targets == ['tests'] and not skipped_or_absent and result == pytest.ExitCode.OK
        payload = merge(earlier, payload, refresh_collection=complete)
    output.write_bytes(gzip.compress(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode(), mtime=0))


def merge(base, supplement, *, refresh_collection=False):
    if (base['revision'], base['ids']) != (supplement['revision'], supplement['ids']):
        raise ValueError('Cannot combine maps for differing source revisions or test IDs')

    def combine_masks(first, second):
        return format(int(first, 16) | int(second, 16), 'x')

    for path, extra in supplement['files'].items():
        entry = base['files'].get(path)
        if entry is None:
            base['files'][path] = extra
            continue
        if entry['source'] != extra['source']:
            raise ValueError(f'Cannot combine differing versions of {path}')
        for line, mask in extra['lines'].items():
            entry['lines'][line] = combine_masks(entry['lines'].get(line, '0'), mask)
        # A complete run separates parent collection from worker activity.
        # Partial traces can add evidence but cannot clear collection flags.
        collection = set(extra['collection'])
        if not refresh_collection:
            collection.update(entry['collection'])
        entry['collection'] = sorted(collection)
        entry['readers'] = combine_masks(entry['readers'], extra['readers'])
    for path, mask in supplement['fixtures'].items():
        base['fixtures'][path] = combine_masks(base['fixtures'].get(path, '0'), mask)
    base['unmapped'] = sorted(set(base['unmapped']) & set(supplement['unmapped']))
    return base


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkout', type=Path)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--target', action='append', help='pytest file or node ID. Defaults to tests')
    parser.add_argument('--merge', type=Path, help='Merge the results into an existing map')
    args = parser.parse_args()
    build_map(args.checkout.resolve(), args.manifest.resolve(), args.output.resolve(),
              args.target or ['tests'], args.merge.resolve() if args.merge else None)
