"""Tests for /api/v1/interfaces endpoints."""
from __future__ import annotations


def test_list_interfaces(client, auth_headers):
    resp = client.get("/api/v1/interfaces", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "eth0" in data
    assert data["eth0"]["description"] == "uplink"


def test_get_interface_found(client, auth_headers):
    resp = client.get("/api/v1/interfaces/eth0", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["description"] == "uplink"


def test_get_interface_not_found(client, auth_headers):
    resp = client.get("/api/v1/interfaces/eth99", headers=auth_headers)
    assert resp.status_code == 404


def test_set_interface_creates_new(client, auth_headers, tmp_store):
    resp = client.post(
        "/api/v1/interfaces/eth1",
        json={"description": "new-port", "mtu": 9000},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "new-port"
    assert data["mtu"] == 9000
    # Candidate is updated; running is unchanged
    assert "eth1" in tmp_store.get_candidate().get("interfaces", {})
    assert "eth1" not in tmp_store.get_running().get("interfaces", {})


def test_set_interface_merges_existing(client, auth_headers, tmp_store):
    resp = client.post(
        "/api/v1/interfaces/eth0",
        json={"mtu": 1500},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    candidate = tmp_store.get_candidate()
    iface = candidate["interfaces"]["eth0"]
    # description preserved, mtu added
    assert iface["description"] == "uplink"
    assert iface["mtu"] == 1500


def test_delete_interface(client, auth_headers, tmp_store):
    resp = client.delete("/api/v1/interfaces/eth0", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["deleted"] == "eth0"
    assert "eth0" not in tmp_store.get_candidate().get("interfaces", {})
    # running unchanged
    assert "eth0" in tmp_store.get_running().get("interfaces", {})


def test_delete_interface_not_found(client, auth_headers):
    resp = client.delete("/api/v1/interfaces/eth99", headers=auth_headers)
    assert resp.status_code == 404
