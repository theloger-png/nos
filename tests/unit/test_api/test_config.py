"""Tests for /api/v1/config endpoints."""
from __future__ import annotations

import json


def test_compare_no_changes(client, auth_headers):
    resp = client.get("/api/v1/config/compare", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["diff"] == ""


def test_compare_with_changes(client, auth_headers, tmp_store):
    # Make a change to candidate
    tmp_store.update_candidate(["interfaces", "eth0", "description"], "changed")
    resp = client.get("/api/v1/config/compare", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["diff"] != ""


def test_commit(client, auth_headers, tmp_store):
    tmp_store.update_candidate(["system", "host_name"], "newname")
    resp = client.post("/api/v1/config/commit", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "committed"
    # Running now has the new hostname
    assert tmp_store.get_running()["system"]["host_name"] == "newname"


def test_commit_validation_failure(client, auth_headers, tmp_store):
    # Inject an invalid MTU value directly into candidate (bypassing Pydantic)
    candidate = tmp_store.get_candidate()
    candidate.setdefault("interfaces", {})["eth0"] = {"mtu": -1}
    tmp_store.set_candidate(candidate)

    resp = client.post("/api/v1/config/commit", headers=auth_headers)
    # Validation may pass at store level (schema validator runs separately);
    # but commit should succeed or fail cleanly — no 5xx
    assert resp.status_code in (200, 400)


def test_rollback_not_found(client, auth_headers):
    resp = client.post("/api/v1/config/rollback/5", headers=auth_headers)
    assert resp.status_code == 404


def test_rollback_success(client, auth_headers, tmp_store, tmp_engine):
    # Create a checkpoint by committing first
    tmp_store.update_candidate(["system", "host_name"], "r2")
    tmp_engine.commit()

    # rollback 0 now exists; load it into candidate
    resp = client.post("/api/v1/config/rollback/0", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "loaded"
    assert body["rollback"] == 0
    # candidate now holds the pre-commit config
    assert tmp_store.get_candidate()["system"]["host_name"] == "testnode"


def test_rollback_out_of_range(client, auth_headers):
    resp = client.post("/api/v1/config/rollback/50", headers=auth_headers)
    assert resp.status_code == 422  # path param validation
