import pytest

def pytest_configure(config):
    config.addinivalue_line("markers", "unit: fast, no external tools")
    config.addinivalue_line("markers", "golden: requires GoldenRefSim, no external tools")
    config.addinivalue_line("markers", "integration: requires Yosys + Quaigh installed")
    config.addinivalue_line("markers", "slow: benchmark circuits, >30s")
    config.addinivalue_line("markers", "phase1: Phase 1+ feature")
    config.addinivalue_line("markers", "phase2: Phase 2+ feature")
    config.addinivalue_line("markers", "sequential: Phase 2.5+ feature")
