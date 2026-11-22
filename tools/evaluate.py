import json
from pathlib import Path
import subprocess
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from score import evaluate_run
result = subprocess.run([sys.executable, str(ROOT / 'selector.py'), '--data', str(ROOT / 'sample'), *sys.argv[1:]], capture_output=True, check=True)
tests = json.loads((ROOT / 'sample/tests.json').read_text('utf-8'))['tests']
answers = json.loads((ROOT / 'sample/answers.json').read_text('utf-8'))['commits']
report = evaluate_run(result.stdout, tests, answers)
(ROOT / 'output.jsonl').write_bytes(result.stdout)
print(json.dumps(report, indent=2))
if not report['ok'] or report['missed_commits']:
    raise SystemExit(1)
