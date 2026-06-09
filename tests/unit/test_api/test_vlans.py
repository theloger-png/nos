"""Tests for /api/v1/vlans endpoints."""
from __future__ import annotations


def test_list_vlans(client, auth_headers):
    resp = client.get("/api/v1/vlans", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "vlan100" in data
    assert data["vlan100"]["vlan_id"] == 100


def test_create_vlan(client, auth_headers, tmp_store):
    resp = client.post(
        "/api/v1/vlans",
        json={"name": "vlan200", "vlan_id": 200, "description": "servers"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    candidate = tmp_store.get_candidate()
    assert "vlan200" in candidate.get("vlans", {})
    assert candidate["vlans"]["vlan200"]["vlan_id"] == 200
    assert candidate["vlans"]["vlan200"]["description"] == "servers"


def test_create_vlan_invalid_id(client, auth_headers):
    resp = client.post(
        "/api/v1/vlans",
        json={"name": "bad", "vlan_id": 5000},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_delete_vlan(client, auth_headers, tmp_store):
    resp = client.delete("/api/v1/vlans/100", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["deleted"] == "vlan100"
    assert "vlan100" not in tmp_store.get_candidate().get("vlans", {})
    # running unchanged
    assert "vlan100" in tmp_store.get_running().get("vlans", {})


def test_delete_vlan_not_found(client, auth_headers):
    resp = client.delete("/api/v1/vlans/999", headers=auth_headers)
    assert resp.status_code == 404
