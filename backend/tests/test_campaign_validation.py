"""Regression: campaign name is limited to 128 chars at API boundary."""
import os

import pytest

os.environ["ALLOW_MOCK_AUTH"] = "true"
os.environ["NODE_ENV"] = "test"

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
pytestmark = pytest.mark.postgres

def _cleanup(campaign_id: str):
    try:
        client.delete(f"/api/campaigns/{campaign_id}")
    except Exception:
        pass

def test_create_name_128_ok():
    name = "a" * 128
    resp = client.post("/api/campaigns", json={"name": name})
    assert resp.status_code == 200, resp.text
    cid = resp.json()["campaign"]["id"]
    _cleanup(cid)

def test_create_name_129_rejected():
    name = "a" * 129
    resp = client.post("/api/campaigns", json={"name": name})
    assert resp.status_code == 400
    assert "128" in resp.json().get("detail", "")

def test_create_empty_rejected():
    resp = client.post("/api/campaigns", json={"name": "   "})
    assert resp.status_code == 400

def test_update_name_129_rejected():
    name = "a" * 10
    resp = client.post("/api/campaigns", json={"name": name})
    assert resp.status_code == 200
    cid = resp.json()["campaign"]["id"]
    try:
        upd = client.put(f"/api/campaigns/{cid}", json={"name": "b" * 129})
        assert upd.status_code == 400
        assert "128" in upd.json().get("detail", "")
        got = client.get(f"/api/campaigns/{cid}")
        assert got.json()["campaign"]["name"] == name
    finally:
        _cleanup(cid)

def test_update_seed_129_rejected():
    resp = client.post("/api/campaigns", json={"name": "seed-test"})
    assert resp.status_code == 200
    cid = resp.json()["campaign"]["id"]
    try:
        upd = client.put(f"/api/campaigns/{cid}", json={"random_seed": "x" * 129})
        assert upd.status_code == 400
    finally:
        _cleanup(cid)
