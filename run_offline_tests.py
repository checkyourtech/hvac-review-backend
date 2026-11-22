"""Bounded offline regression runner. No dotenv, provider calls or repo writes.

Requires Node on PATH for the accepted browser regressions. All results and
synthetic storage are private temporary files. No installed packages are changed.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def run_child(output, names):
    import unittest
    from unittest.mock import patch
    sys.dont_write_bytecode = True
    os.chdir(ROOT)
    tempfile.tempdir = str(output)
    violations = []

    def audit(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname', 'socket.sendto'):
            violations.append('network')
            raise RuntimeError('Networking prohibited during offline regression tests')
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            original = Path(os.fsdecode(args[0]))
            path = Path(os.path.abspath(original))
            if path.name == '.env' or ROOT / 'uploads' in path.parents or ROOT / 'backups' in path.parents:
                violations.append('protected path')
                raise RuntimeError('Private customer/configuration files are not test inputs')
            mode, flags = args[1:3]
            write = ((isinstance(mode, str) and any(c in mode for c in 'wax+')) or
                     (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
            # Existing storage adapters use directory-FD-relative private writes.
            relative = not original.is_absolute() and re.fullmatch(
                r'(?:backup\.json|ledger\.sqlite3|store\.json|\.lock|manifest\.json|\d{4}\.bin|[A-Za-z0-9_.-]+\.queue\.lock)', str(original))
            if write and not relative and path != Path(os.devnull) and output not in path.parents:
                violations.append('non-temporary write')
                raise RuntimeError('Test outputs must remain in temporary storage')

    sys.addaudithook(audit)
    environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG') if key in os.environ}
    environment.update(OPENAI_API_KEY='offline-dummy', SERPAPI_API_KEY='offline-dummy',
        EMAIL_APP_PASSWORD='', PYTHON_DOTENV_DISABLED='1', PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(output))
    names = names or sorted(path.stem for path in ROOT.glob('test_*.py'))
    with patch.dict(os.environ, environment, clear=True), patch('dotenv.load_dotenv', return_value=False):
        with (output / 'tests.log').open('w') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            suite = unittest.defaultTestLoader.loadTestsFromNames(names)
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    summary = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   skipped=len(result.skipped), prohibited_attempts=violations,
                   success=result.wasSuccessful() and not violations and not result.skipped)
    (output / 'results.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)
    return 0 if summary['success'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tests', nargs='*', help='Optional unittest module/test names; default all test_*.py')
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--child-output', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    if args.child_output:
        output = args.child_output.resolve()
        if Path(tempfile.gettempdir()).resolve() not in output.parents:
            parser.error('Test output must be temporary')
        return run_child(output, args.tests)
    if not 1 <= args.timeout <= 1800:
        parser.error('Timeout must be between 1 and 1800 seconds')
    if shutil.which('node') is None:
        parser.error('Node.js must be on PATH; browser regressions must not be skipped')
    output = Path(tempfile.mkdtemp(prefix='cyt-offline-')).resolve()
    print('Offline test output: ' + str(output), flush=True)
    process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
        '--child-output', str(output), *args.tests], start_new_session=True)
    try:
        return process.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        print('Offline tests exceeded the bounded timeout; inspect temporary results.', flush=True)
        return 124


if __name__ == '__main__':
    raise SystemExit(main())
