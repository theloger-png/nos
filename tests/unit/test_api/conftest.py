"""Shared fixtures for NOS API tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nos.api.app import create_app
from nos.api import auth as auth_module
from nos.api import deps as deps_module
from nos.config.commit import CommitEngine
from nos.config.store import ConfigStore


_VALID_KEY = "test-api-key-0000000000000000000000000000000000000000000000000000"


@pytest.fixture()
def tmp_store(tmp_path):
    """A ConfigStore backed by a temporary directory with minimal running config."""
    (tmp_path / "config" / "rollback").mkdir(parents=True)
    running = tmp_path / "config" / "running.json"
    running.write_text(
        '{"system": {"host_name": "testnode"}, '
        '"interfaces": {"eth0": {"description": "uplink"}}, '
        '"vlans": {"vlan100": {"vlan_id": 100}}, '
        '"routing_options": {"static": {"route": {"10.0.0.0/24": {"next_hop": "192.168.1.1"}}}}}'
    )
    store = ConfigStore(base_dir=tmp_path)
    return store


@pytest.fixture()
def tmp_engine(tmp_store):
    return CommitEngine(tmp_store)


@pytest.fixture()
def client(tmp_store, tmp_engine, monkeypatch):
    """TestClient with auth stubbed out and deps pointing at tmp_store/tmp_engine."""
    # Override auth to accept _VALID_KEY
    monkeypatch.setattr(auth_module, "_cached_key", _VALID_KEY)

    # Override deps to return our tmp fixtures
    monkeypatch.setattr(deps_module, "_get_store", lambda: tmp_store)
    monkeypatch.setattr(deps_module, "_get_engine", lambda: tmp_engine)

    app = create_app()

    # Patch FastAPI dependency overrides
    from nos.api import deps
    app.dependency_overrides[deps.get_store] = lambda: tmp_store
    app.dependency_overrides[deps.get_engine] = lambda: tmp_engine

    return TestClient(app)


@pytest.fixture()
def auth_headers():
    return {"X-API-Key": _VALID_KEY}
