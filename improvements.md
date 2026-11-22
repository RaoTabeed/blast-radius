# Blast Radius improvements

## Repository and scoring in 10 lines

1. The Docker entry point runs `selector.py` on `/data/tests.json` and `/data/commits/*.patch`.
2. Each patch is an independent unified diff against SQLGlot revision `eb11aec4`.
3. The manifest supplies test IDs, numeric test numbers, and measured durations.
4. `tools/build_impact.py` records per-test Python coverage and source and fixture reads on the pinned checkout.
5. `tools/trace_worker.py` captures coverage for optimizer and executor work in subprocesses.
6. `impact.py` validates hunks against the pinned source and looks up affected tests by line, scope, or fixture.
7. The selector includes unmapped tests and defaults to the full suite when impact is uncertain.
8. The scheduler balances selected tests across up to eight workers and emits one JSON object per patch.
9. A patch costs its longest worker load plus two seconds per worker, and a missed patch adds 300 seconds.
10. The sample scorer sums those costs. The leaderboard adds container CPU time and reports the median of three runs.

## Baseline and attempts

The baseline at `451b3ac` was 315.010 simulated seconds with zero missed patches on the 30 official samples. Both `python tools/evaluate.py` and `make sample` reproduced it. Five full-suite fallbacks cost 184.085 seconds. The slowest individual test was `test_max_nodes` at 28.817 seconds, followed by `test_execute_tpcds` at 21.454 seconds. Every nonempty baseline schedule already met the lower bound from its longest test and total worker load.

| Attempt | Before | After | Misses | Outcome |
| --- | ---: | ---: | ---: | --- |
| Follow reads for a value-only dialect `WEEK_OFFSET` edit and keep tests that executed the definition | 315.010 s | 283.454 s | 0 | Kept |
| Follow MySQL-family `SHOW_PARSERS` closure calls for literal option edits that preserve dispatch keys | 283.454 s | 248.706 s | 0 | Kept |
| Follow numeric escape decoding calls for tokenizer table edits that preserve keys and valid tuple shapes | 248.706 s | 219.065 s | 0 | Kept |

The week-offset rule reduced one full-suite fallback to 12 tests. It covers every pinned read site, including readers on other dialects, and falls back if a read site lacks test coverage or runs during collection.

The `SHOW_PARSERS` rule reduced another full-suite fallback to 20 tests. The dictionaries still build the same tries. The selector includes every test that called the affected closure factory's returned functions or executed the edited definition.

The numeric escape rule reduced another full-suite fallback to 28 tests. The selector keeps tests covering the decoder call across all dialects and tests that executed the edited definition. Keys stay fixed because they also affect dialect initialization and SQL generation.
