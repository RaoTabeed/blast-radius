"""Conservative selector with adaptive scheduling. No sample answers are read."""
import argparse
import json
from pathlib import Path

from impact import ImpactMap


def schedule(tests):
    if not tests:
        return []
    ordered = sorted(tests, key=lambda t: (-round(t['seconds'] * 1000), t['number']))
    best, best_cost = [], float('inf')
    for count in range(1, min(8, len(tests)) + 1):
        workers = [[] for _ in range(count)]
        loads = [0] * count
        for test in ordered:
            worker = min(range(count), key=lambda i: (loads[i], i))
            workers[worker].append(test['number'])
            loads[worker] += round(test['seconds'] * 1000)
        workers = [w for w in workers if w]
        cost = max(loads) + 2000 * len(workers)
        if cost < best_cost:
            best, best_cost = workers, cost
    return best


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path('/data'))
    parser.add_argument('--impact-map', type=Path, default=Path(__file__).with_name('impact-map.json.gz'))
    args = parser.parse_args()
    tests = json.loads((args.data / 'tests.json').read_text('utf-8'))['tests']
    by_number = {test['number']: test for test in tests}
    impact = ImpactMap(args.impact_map, tests) if args.impact_map.is_file() else None
    full_schedule = schedule(tests)
    for patch in sorted((args.data / 'commits').glob('*.patch')):
        numbers = None
        if impact is not None:
            try:
                numbers = impact.select(patch.read_text('utf-8'))
            except UnicodeDecodeError:
                pass  # A binary or non-UTF-8 diff may affect tests outside the map.
        workers = full_schedule if numbers is None else schedule([by_number[number] for number in numbers])
        print(json.dumps({'commit': patch.stem, 'workers': workers}, separators=(',', ':')))


if __name__ == '__main__':
    main()
