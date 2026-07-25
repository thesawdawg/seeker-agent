"""
Pytest wrapper over the scenario scripts in tests/scenarios/.

The scenarios are standalone scripts: they assert at module level and mutate
module globals (notably core.argument_tree.DB_PATH) to point at their own temp
database. Importing them all into one pytest process would let them stomp each
other, so each runs in its own subprocess and is judged on exit code.
"""
import subprocess
import sys
from pathlib import Path

import pytest

SCENARIO_DIR = Path(__file__).parent / "scenarios"
REPO_ROOT    = Path(__file__).parent.parent

SCENARIOS = sorted(p.name for p in SCENARIO_DIR.glob("scenario_*.py"))


def test_scenarios_are_discovered():
    """Guard against this suite silently going empty — the failure mode we just fixed."""
    assert SCENARIOS, f"no scenario scripts found in {SCENARIO_DIR}"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_scenario(scenario):
    result = subprocess.run(
        [sys.executable, str(SCENARIO_DIR / scenario)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(
            f"{scenario} exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
