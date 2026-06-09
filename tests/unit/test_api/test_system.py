"""Tests for /api/v1/system endpoints."""
from __future__ import annotations


def test_system_status(client, auth_headers):
    resp = client.get("/api/v1/system/status", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["hostname"] == "testnode"
    assert isinstance(data["uptime_seconds"], int)
    assert data["uptime_seconds"] >= 0
    assert "version" in data


def test_system_forwarding(client, auth_headers):
    resp = client.get("/api/v1/system/forwarding", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    # Returns a dict with "interfaces" key; content depends on environment
    assert "interfaces" in data
    assert isinstance(data["interfaces"], dict)
