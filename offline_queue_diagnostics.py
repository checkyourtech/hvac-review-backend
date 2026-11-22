"""Test-only diagnostics. Production does not import this module.

No request arguments, proposal contents, credentials or exception messages are
captured. The SDK boundary fails before networking even if an upper mock escapes.
"""
import sys
import threading
import traceback
from pathlib import Path
from unittest.mock import patch


class QueueDiagnostics:
    def __init__(self, stack):
        self.events = []
        self.guard = stack.enter_context(patch('openai._base_client.SyncAPIClient.request', side_effect=self.block))
        self.stack = stack

    def block(self, *args, **kwargs):
        self.events.append({'boundary': 'unmocked_openai_client', 'frames': self.frames()})
        raise RuntimeError('Offline test reached an unmocked OpenAI client boundary')

    @staticmethod
    def frames(error=None):
        frames = traceback.extract_tb(error.__traceback__) if error else traceback.extract_stack()[:-1]
        return [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name} for frame in frames[-12:]]

    def observe(self, runtime):
        original = runtime.records.fail_claim
        def failure(*args, **kwargs):
            error = sys.exc_info()[1]
            if error is not None:
                self.events.append({'boundary': 'worker_failure', 'type': type(error).__name__, 'frames': self.frames(error)})
            return original(*args, **kwargs)
        self.stack.enter_context(patch.object(runtime.records, 'fail_claim', side_effect=failure))

    def snapshot(self, ledger):
        result = {'events': self.events, 'threads': [t.name for t in threading.enumerate()
                  if t.name.startswith(('cyt-queue-', 'review-worker'))]}
        try:
            with ledger._connect() as db:
                result['jobs'] = db.execute("""SELECT j.state,j.attempts,j.error_code,j.delivery_state,
                    j.delivery_attempts,q.phase,q.worker_active,q.external_started FROM review_jobs j
                    LEFT JOIN review_queue q USING(job_id)""").fetchall()
        except Exception as error:
            result['storage_error_type'] = type(error).__name__
        return result
