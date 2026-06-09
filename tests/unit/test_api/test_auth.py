"""Tests for API key authentication."""
from __future__ import annotations


def test_missing_key_returns_401(client):
    resp = client.get("/api/v1/interfaces")
    assert resp.status_code == 401
    assert "X-API-Key" in resp.json()["detail"]


def test_wrong_key_returns_401(client):
    resp = client.get("/api/v1/interfaces", headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401
    assert "Invalid" in resp.json()["detail"]


def test_correct_key_returns_200(client, auth_headers):
    resp = client.get("/api/v1/interfaces", headers=auth_headers)
    assert resp.status_code == 200


def test_key_generated_if_missing(tmp_path, monkeypatch):
    """API key file is created on first call when it does not exist."""
    key_path = tmp_path / "api_key"
    monkeypatch.setattr("nos.api.auth._API_KEY_PATH", key_path)
    monkeypatch.setattr("nos.api.auth._cached_key", None)

    from nos.api.auth import _load_or_create_key, reset_key_cache
    reset_key_cache()

    key = _load_or_create_key()
    assert key_path.exists()
    assert key_path.read_text().strip() == key
    assert len(key) == 64  # 32 bytes hex

    reset_key_cache()  # cleanup


def test_key_loaded_from_existing_file(tmp_path, monkeypatch):
    """API key is read from the file when it already exists."""
    key_path = tmp_path / "api_key"
    key_path.write_text("my-existing-key")
    monkeypatch.setattr("nos.api.auth._API_KEY_PATH", key_path)
    monkeypatch.setattr("nos.api.auth._cached_key", None)

    from nos.api.auth import _load_or_create_key, reset_key_cache
    reset_key_cache()

    key = _load_or_create_key()
    assert key == "my-existing-key"

    reset_key_cache()
