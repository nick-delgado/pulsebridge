"""Fixtures for every seed test. The helpers themselves are in ``pulsebridge_seed.testing``."""

import pytest
from pulsebridge_seed.testing import logging_restored


@pytest.fixture(autouse=True)
def _root_logging_restored():
    """The CLI adds a root log handler; no test may leave one for the next (another service's
    tests included: ``pytest services/seed services/health-sync`` runs them in one process)."""
    with logging_restored():
        yield
