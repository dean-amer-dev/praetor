"""Unit tests for webhooks/dispatch_api.py — auth, routing, status."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


API_KEY = "test-praetor-key"


@pytest.fixture
def client(mock_hatchet, monkeypatch):
    monkeypatch.setenv("PRAETOR_API_KEY", API_KEY)
    monkeypatch.setenv("MEM0_BASE_URL", "https://mem0.test")
    monkeypatch.setenv("MEM0_API_KEY", "mem0-key")
    with patch("webhooks.vikunja.register_webhook_on_startup", new=AsyncMock()):
        from webhooks.app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c, mock_hatchet


def _auth(key: str = API_KEY) -> dict:
    return {"Authorization": f"Bearer {key}"}


class TestAuth:
    def test_missing_token_returns_403(self, client):
        c, _ = client
        resp = c.post("/api/v1/dispatch", json={"title": "t", "type": "research"})
        assert resp.status_code in (401, 403)

    def test_wrong_token_returns_401(self, client):
        c, _ = client
        resp = c.post(
            "/api/v1/dispatch",
            json={"title": "t", "type": "research"},
            headers=_auth("wrong-key"),
        )
        assert resp.status_code == 401

    def test_correct_token_accepted(self, client):
        c, _ = client
        resp = c.post(
            "/api/v1/dispatch",
            json={"title": "t", "type": "research"},
            headers=_auth(),
        )
        assert resp.status_code == 200


class TestDispatch:
    def test_research_dispatches_agent_research(self, client):
        c, hatchet = client
        resp = c.post(
            "/api/v1/dispatch",
            json={"title": "Research X", "description": "details", "type": "research"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["event"] == "agent:research"
        assert "task_id" in data
        assert data["hatchet_url"] == "https://hatchet.amer.dev"
        hatchet.event.push.assert_called_once()
        assert hatchet.event.push.call_args[0][0] == "agent:research"

    def test_code_dispatches_agent_code(self, client):
        c, hatchet = client
        resp = c.post(
            "/api/v1/dispatch",
            json={"title": "Code task", "type": "code"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["event"] == "agent:code"

    def test_pipeline_dispatches_pipeline(self, client):
        c, hatchet = client
        resp = c.post(
            "/api/v1/dispatch",
            json={"title": "Pipeline task", "type": "pipeline"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["event"] == "pipeline:research_code"

    def test_invalid_type_returns_422(self, client):
        c, _ = client
        resp = c.post(
            "/api/v1/dispatch",
            json={"title": "t", "type": "bad_type"},
            headers=_auth(),
        )
        assert resp.status_code == 422

    def test_payload_forwarded_to_hatchet(self, client):
        c, hatchet = client
        resp = c.post(
            "/api/v1/dispatch",
            json={"title": "My Task", "description": "My Desc", "type": "research"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        push_payload = hatchet.event.push.call_args[0][1]
        assert push_payload["task_title"] == "My Task"
        assert push_payload["task_description"] == "My Desc"
        assert "task_id" in push_payload

    def test_no_vikunja_task_by_default(self, client):
        c, _ = client
        resp = c.post(
            "/api/v1/dispatch",
            json={"title": "t", "type": "research"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["vikunja_task_id"] is None
        assert data["vikunja_task_url"] is None


class TestStatus:
    def test_status_done_when_mem0_has_results(self, client):
        c, _ = client
        mem0_response = {"results": [{"memory": "Research summary here"}]}
        with patch("webhooks.dispatch_api._poll_mem0", new=AsyncMock(return_value="Research summary here")):
            resp = c.get("/api/v1/status/12345", headers=_auth())
        assert resp.status_code == 200
        data = resp.json()
        assert data["done"] is True
        assert data["mem0_summary"] == "Research summary here"
        assert data["task_id"] == 12345

    def test_status_not_done_when_mem0_empty(self, client):
        c, _ = client
        with patch("webhooks.dispatch_api._poll_mem0", new=AsyncMock(return_value=None)):
            resp = c.get("/api/v1/status/99999", headers=_auth())
        assert resp.status_code == 200
        data = resp.json()
        assert data["done"] is False
        assert data["mem0_summary"] is None

    def test_status_auth_required(self, client):
        c, _ = client
        resp = c.get("/api/v1/status/12345")
        assert resp.status_code in (401, 403)
