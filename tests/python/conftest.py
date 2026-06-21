import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Allow tests in subdirectories to import helpers (campaign_fixtures, soc2_fixtures,
# db_v3_helpers) that live at tests/python/ root with bare module names.
TESTS_PYTHON = Path(__file__).resolve().parent
if str(TESTS_PYTHON) not in sys.path:
    sys.path.insert(0, str(TESTS_PYTHON))


@pytest.fixture(scope="module")
def require_cpp_core() -> None:
    import faultflow.runner.runner as runner_mod

    if runner_mod._load_core() is None:
        pytest.skip("C++ extension _faultflow_core is required")


def pytest_configure(config):
    config.addinivalue_line("markers", "unit: fast, no external tools")
    config.addinivalue_line(
        "markers", "golden: requires GoldenRefSim, no external tools"
    )
    config.addinivalue_line("markers", "integration: requires external tools")
    config.addinivalue_line("markers", "slow: benchmark circuits, >30s")
    config.addinivalue_line("markers", "verification: vector verification gate")
    config.addinivalue_line("markers", "sequential: sequential feature")
