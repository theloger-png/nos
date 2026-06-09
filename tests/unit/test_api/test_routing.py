"""Tests for /api/v1/routes endpoints."""
from __future__ import annotations

import urllib.parse


def test_list_routes(client, auth_headers):
    resp = client.get("/api/v1/routes", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "10.0.0.0/24" in data


def test_add_static_route(client, auth_headers, tmp_store):
    resp = client.post(
        "/api/v1/routes/static",
        json={"prefix": "172.16.0.0/12", "next_hop": "10.0.0.1"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    candidate = tmp_store.get_candidate()
    routes = candidate["routing_options"]["static"]["route"]
    assert "172.16.0.0/12" in routes
    assert routes["172.16.0.0/12"]["next_hop"] == "10.0.0.1"


def test_add_static_route_invalid_prefix(client, auth_headers):
    resp = client.post(
        "/api/v1/routes/static",
        json={"prefix": "not-a-prefix"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_delete_static_route(client, auth_headers, tmp_store):
    encoded = urllib.parse.quote("10.0.0.0/24", safe="")
    resp = client.delete(f"/api/v1/routes/static/{encoded}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["deleted"] == "10.0.0.0/24"
    candidate = tmp_store.get_candidate()
    routes = candidate.get("routing_options", {}).get("static", {}).get("route", {})
    assert "10.0.0.0/24" not in routes
    # running unchanged
    running_routes = tmp_store.get_running()["routing_options"]["static"]["route"]
    assert "10.0.0.0/24" in running_routes


def test_delete_static_route_not_found(client, auth_headers):
    encoded = urllib.parse.quote("1.2.3.0/24", safe="")
    resp = client.delete(f"/api/v1/routes/static/{encoded}", headers=auth_headers)
    assert resp.status_code == 404
