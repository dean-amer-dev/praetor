"""Integration tests: full FastAPI app via TestClient (no live cluster needed)."""
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _sig_vikunja(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _sig_github(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def app_client():
    """Full app with startup hook and Hatchet stubbed out."""
    mock_hatchet = MagicMock()
    mock_hatchet.event.push = MagicMock()

    with (
        patch("webhooks.vikunja._get_hatchet", return_value=mock_hatchet),
        patch("webhooks.github._get_hatchet", return_value=mock_hatchet),
        patch("webhooks.vikunja.register_webhook_on_startup", new=AsyncMock()),
    ):
        from webhooks.app import app
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client, mock_hatchet


class TestHealthz:
    def test_healthz_returns_ok(self, app_client):
        client, _ = app_client
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestRoutesExist:
    def test_vikunja_route_exists(self, app_client):
        client, _ = app_client
        resp = client.post("/webhooks/vikunja", json={})
        assert resp.status_code != 404

    def test_github_route_exists(self, app_client):
        client, _ = app_client
        resp = client.post("/webhooks/github", json={})
        assert resp.status_code != 404


class TestVikunjaWebhookIntegration:
    def test_research_label_triggers_dispatch(self, app_client):
        client, hatchet = app_client
        payload = {
            "event_type": "task.updated",
            "data": {"task": {"id": 10, "title": "Research X", "description": "", "labels": [{"id": 14}]}},
        }
        resp = client.post("/webhooks/vikunja", json=payload)
        assert resp.status_code == 200
        assert "agent:research" in resp.json()["dispatched"]
        hatchet.event.push.assert_called()

    def test_pipeline_both_labels(self, app_client):
        client, hatchet = app_client
        payload = {
            "event_type": "task.updated",
            "data": {"task": {"id": 20, "title": "Pipeline task", "description": "", "labels": [{"id": 14}, {"id": 11}]}},
        }
        resp = client.post("/webhooks/vikunja", json=payload)
        assert resp.status_code == 200
        assert resp.json()["dispatched"] == ["pipeline:research_code"]

    def test_invalid_json_returns_400(self, app_client):
        client, _ = app_client
        resp = client.post(
            "/webhooks/vikunja",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_no_secret_no_sig_passes(self, app_client, monkeypatch):
        monkeypatch.delenv("VIKUNJA_WEBHOOK_SECRET", raising=False)
        client, _ = app_client
        payload = {"event_type": "task.updated", "data": {"task": {"id": 1, "labels": []}}}
        resp = client.post("/webhooks/vikunja", json=payload)
        assert resp.status_code == 200


class TestGitHubWebhookIntegration:
    def test_ping_event(self, app_client):
        client, _ = app_client
        resp = client.post(
            "/webhooks/github",
            json={},
            headers={"X-GitHub-Event": "ping"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "pong"}

    def test_pr_opened_dispatches(self, app_client):
        client, hatchet = app_client
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 7,
                "html_url": "https://github.com/amerenda/ecdysis/pull/7",
                "diff_url": "https://github.com/amerenda/ecdysis/pull/7.diff",
                "user": {"login": "human"},
            },
            "repository": {"full_name": "amerenda/ecdysis"},
        }
        resp = client.post(
            "/webhooks/github",
            json=payload,
            headers={"X-GitHub-Event": "pull_request"},
        )
        assert resp.status_code == 200
        assert resp.json()["dispatched"] == "github:pr_opened"

    def test_invalid_github_sig_rejected(self, app_client, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret123")
        client, _ = app_client
        resp = client.post(
            "/webhooks/github",
            content=b'{"action":"opened"}',
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": "sha256=badhash",
            },
        )
        assert resp.status_code == 401

    def test_missing_sig_with_secret_rejected(self, app_client, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret123")
        client, _ = app_client
        resp = client.post(
            "/webhooks/github",
            json={"action": "opened"},
            headers={"X-GitHub-Event": "pull_request"},
        )
        assert resp.status_code == 401
