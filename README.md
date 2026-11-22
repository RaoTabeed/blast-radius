# Blast Radius

A regression test selector for the [RWX Blast Radius track](https://www.joinbuilders.org/buildathon/tracks/blast-radius) at the Builders Buildathon. Given a patch to SQLGlot and a test manifest, it selects the tests most likely to be affected and assigns them to up to eight workers.

## Local validation

| Run | Simulated time | Missed patches |
| --- | ---: | ---: |
| Earlier full-suite baseline on the 30 official samples | 1,104.510 s | 0 |
| Original coverage selector in Linux Docker on the same samples | 315.010 s | 0 |
| Current selector in Linux Docker on the same samples | **207.702 s** | **0** |

The Docker run used the track's sample limits. The sample scorer reports simulated time and misses. The competition scorer also charges CPU time and evaluates unseen patches. See [improvements.md](improvements.md) for the experiment and validation results.

In a separate check, disabling identifier normalization caused 46 test IDs to fail. The selector included all 46 among the 309 tests it selected. Coverage from the original checkout is an estimate of impact. Changes that alter imports, dynamic dispatch, or dependencies can require a wider selection.

## How it works

1. **Build the dependency map.** Record which tests execute each source line or read each fixture and source file on pinned SQLGlot. Include Python worker processes started by optimizer and executor tests. The bundled map covers 1,620 test IDs, 183 source files, and 62 fixture paths.
2. **Select tests for a patch.** Check each diff against the pinned source, look up coverage for affected statements or functions, and combine the results across changed files. Include tests that reached any operand when an edit could change short-circuit behavior. For supported value-only edits to dialect and parser settings, follow coverage to where the values are consumed. Use the full suite when the patch or its import-time effects cannot be mapped with enough confidence.
3. **Schedule the selected tests.** Try one through eight workers. For each worker count, place the longest test first on the least busy worker. Choose the schedule with the lowest simulated cost, including the two-second charge per worker.

The container reads `/data/tests.json` and independent patches under `/data/commits/`. It prints one JSON object per patch on stdout, using the numerical test IDs from the manifest. For example:

```json
{"commit":"k7q2","workers":[[12,845],[3,77]]}
```

The container uses the bundled map offline. It receives the patches and manifest, while the host scorer reads `sample/answers.json`. See [SCORING.md](SCORING.md) for the output contract and scoring rules.

## Run the official sample

From the repository root, with Docker running and `python3` available:

```sh
make sample
```

On Windows Command Prompt with Docker, Python, and MinGW Make on `PATH`, run:

```cmd
mingw32-make sample
```

This builds the `linux/amd64` image, runs the selector with the sample input mounted read-only, saves its JSONL output to `output.jsonl`, and scores it. It runs the selector as an unprivileged user with networking disabled.

For quicker development without Docker:

```sh
python -m unittest discover -s tests -v
python tools/evaluate.py
```

## Rebuild the dependency map

Use a separate, clean [SQLGlot checkout at `eb11aec4`](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa). From this repository's root, install the starter kit's test environment and Coverage.py in a Python virtual environment, then run the generator. Replace `/path/to/sqlglot` with your checkout path.

```sh
python -m pip install -r requirements.lock coverage==7.17.4
python tools/build_impact.py /path/to/sqlglot sample/tests.json impact-map.json.gz
```

The generator runs tests against the pinned checkout and uses the manifest to map pytest node IDs to the recorded coverage. Pass `--merge /path/to/previous-map.json.gz` to retain the previously observed per-test dependencies. A complete passing run refreshes collection flags when merging. Docker builds copy the resulting map into the image.

## License and credits

This project is released under the [MIT License](LICENSE), as required by the [Builders Buildathon rules](https://www.joinbuilders.org/buildathon#rules). The Builders/RWX starter kit provided the original Dockerfile, Makefile, scoring tools, test environment lock, and sample data. The dependency map includes source from the pinned SQLGlot revision, whose original MIT notice is preserved in [SQLGLOT-LICENSE](SQLGLOT-LICENSE).

The dependency mapping approach draws on [Ekstazi's work on dynamic file dependencies](https://users.ece.utexas.edu/~gligoric/papers/GligoricETAL15Ekstazi.pdf) and [Coverage.py's per-test measurement contexts](https://coverage.readthedocs.io/en/7.14.3/contexts.html).
