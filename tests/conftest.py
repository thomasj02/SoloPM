"""Shared pytest fixtures."""

import pytest

from solopm.core.service import Service
from solopm.core.store import Store


@pytest.fixture
def service(tmp_path):
    """A Service backed by a fresh, initialized SQLite store in a temp dir."""
    store = Store(tmp_path / "solopm.db")
    store.init()
    return Service(store)


@pytest.fixture
def project(service):
    """A registered project to hang tickets off of."""
    return service.add_project(key="SOLO", name="SoloPM", repo="/tmp/solopm", master="main")
