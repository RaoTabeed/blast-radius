"""Record the SQLGlot code run by pytest's process-pool tasks."""
from multiprocessing.util import Finalize

import coverage

_trace = None


def _save(trace):
    trace.stop()
    trace.save()


def traced_call(filename, source, nodeid, func, args, kwargs):
    global _trace
    if _trace is None:
        _trace = coverage.Coverage(data_file=filename, data_suffix=True, source=[source])
        _trace.start()
        Finalize(None, _save, args=(_trace,), exitpriority=1)
    _trace.switch_context(nodeid)
    try:
        return func(*args, **kwargs)
    finally:
        _trace.switch_context('')
