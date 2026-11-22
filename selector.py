"""Conservative selector with adaptive scheduling. No sample answers are read."""
import argparse
import json
from pathlib import Path


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
    args = parser.parse_args()
    tests = json.loads((args.data / 'tests.json').read_text('utf-8'))['tests']
    workers = schedule(tests)
    for patch in sorted((args.data / 'commits').glob('*.patch')):
        print(json.dumps({'commit': patch.stem, 'workers': workers}, separators=(',', ':')))


if __name__ == '__main__':
    main()
