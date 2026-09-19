"""
Peer-review T1: every tests/test_*.py file already has correct pass/fail logic of its own
(a `failures` list, printed PASS/FAIL lines, and sys.exit(1) when anything failed) -- but
none of that was ever pytest-discoverable, since check() only appends to a list instead of
asserting. Anyone who ran `pytest` (rather than `python3 tests/test_X.py`, the only way this
suite has ever actually been run/verified) got every file reported green regardless of the
real outcome.

This file is the bridge, not a rewrite of 24 files' worth of working, already-proven check()
logic: it runs each other test_*.py exactly the way it has always been run (a subprocess,
same as `python3 tests/test_X.py`) and asserts that subprocess's own exit code. That makes
`pytest tests/` genuinely fail -- with the full captured PASS/FAIL output attached -- exactly
when a file's own logic says it should, without touching a single one of those already-correct
files.

Two files are deliberately excluded from this automated gate -- both make real calls to live
external APIs (Groq/Sarvam), so a green run isn't reproducible offline and a red run doesn't
necessarily mean a code regression (an expired trial key, e.g. the "No credits available"
402 seen live this session, fails them exactly as loudly as a real bug would). Including them
here would make this gate permanently red for reasons that have nothing to do with whether the
code is actually broken -- see docs/ or the peer-review notes (finding T2) for the follow-up
to properly split these into a marked integration suite instead of silently excluding them
forever:
  - test_nlu_integration.py -- live Groq/Sarvam classification calls
  - test_prompt_isolation.py -- live Sarvam calls; also has its own separate, not-yet-fixed
    issue (T2) where it never reports failure via its exit code at all

Run directly: pytest tests/test_all_scripts_pass.py -v
(or just `pytest tests/`, which picks this up like any other test module)
"""

import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent

_EXCLUDED = {
    Path(__file__).name,  # this file itself -- not a subprocess-runnable script
    "test_nlu_integration.py",
    "test_prompt_isolation.py",
}


def _standalone_test_scripts():
    return sorted(
        (p for p in TESTS_DIR.glob("test_*.py") if p.name not in _EXCLUDED),
        key=lambda p: p.name,
    )


@pytest.mark.parametrize("script", _standalone_test_scripts(), ids=lambda p: p.name)
def test_script_passes(script):
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=TESTS_DIR.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
