"""Fixtures for the health-sync tests. The helpers themselves are in ``health_sync.testing``."""

from pathlib import Path

import pytest
from health_sync.testing import World


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)
