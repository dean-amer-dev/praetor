"""Unit tests for webhooks/github.py — HMAC verification and event routing."""
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from webhooks.github import _verify_signature


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sig(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _pr_payload(action: str = "opened", repo: str = "amerenda/ecdysis", pr_number: int = 42) -> dict:
    return {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "html_url": f"https://github.com/{repo}/pull/{pr_number}",
            "diff_url": f"https://github.com/{repo}/pull/{pr_number}.diff",
            "user": {"login": "human-dev"},
        },
        "repository": {"full_name": repo},
    }


# ---------------------------------------------------------------------------
# Signature verification (pure function)
# ---------------------------------------------------------------------------

class TestVerifySignature:
    def test_valid_signature_accepted(self, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "ghs")
        body = b'{"test": 1}'
        _verify_signature(body, _sig(body, "ghs"))  # must not raise

    def test_invalid_signature_raises_401(self, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "ghs")
        with pytest.raises(HTTPException) as exc:
            _verify_signature(b"body", "sha256=badhash")
        assert exc.value.status_code == 401

    def test_missing_header_with_secret_raises_401(self, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "ghs")
        with pytest.raises(HTTPException) as exc:
            _verify_signature(b"body", None)
        assert exc.value.status_code == 401

    def test_no_secret_configured_passes(self, monkeypatch):
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        # Should pass through without raising regardless of header
        _verify_signature(b"body", None)
        _verify_signature(b"body", "sha256=anything")


# ---------------------------------------------------------------------------
# Webhook routing via TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def client(mock_hatchet):
    with patch("webhooks.vikunja.register_webhook_on_startup", new=AsyncMock()):
        from webhooks.app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c, mock_hatchet


def _post(client, payload: dict, event: str = "pull_request", secret: str | None = None) -> "httpx.Response":
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "X-GitHub-Event": event}
    if secret:
        headers["X-Hub-Signature-256"] = _sig(body, secret)
    return client.post("/webhooks/github", content=body, headers=headers)


class TestGitHubRouting:
    def test_ping_returns_pong(self, client):
        c, _ = client
        resp = _post(c, {}, event="ping")
        assert resp.status_code == 200
        assert resp.json() == {"status": "pong"}

    def test_unknown_event_ignored(self, client):
        c, hatchet = client
        resp = _post(c, {}, event="push")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        hatchet.event.push.assert_not_called()

    def test_pr_review_disabled_by_default(self, client):
        """
        Auto-review-on-PR is disabled: cancelled Hatchet steps don't abort the
        in-flight LiteLLM/vLLM request, so a timed-out review permanently occupied
        the single-session backend's one execution slot. Every pull_request action
        must short-circuit to "disabled" without dispatching, until re-enabled.
        """
        c, hatchet = client
        resp = _post(c, _pr_payload(action="opened"))
        assert resp.status_code == 200
        assert resp.json() == {"status": "disabled"}
        hatchet.event.push.assert_not_called()

    def test_pr_synchronize_ignored(self, client, monkeypatch):
        monkeypatch.setattr("webhooks.github._PR_REVIEW_ENABLED", True)
        c, hatchet = client
        resp = _post(c, _pr_payload(action="synchronize"))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        hatchet.event.push.assert_not_called()

    def test_pr_closed_ignored(self, client, monkeypatch):
        monkeypatch.setattr("webhooks.github._PR_REVIEW_ENABLED", True)
        c, hatchet = client
        resp = _post(c, _pr_payload(action="closed"))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_pr_opened_dispatches_event(self, client, monkeypatch):
        monkeypatch.setattr("webhooks.github._PR_REVIEW_ENABLED", True)
        c, hatchet = client
        resp = _post(c, _pr_payload(action="opened"))
        assert resp.status_code == 200
        assert resp.json()["dispatched"] == "github:pr_opened"
        hatchet.event.push.assert_called_once()
        assert hatchet.event.push.call_args[0][0] == "github:pr_opened"

    def test_pr_payload_fields_forwarded(self, client, monkeypatch):
        monkeypatch.setattr("webhooks.github._PR_REVIEW_ENABLED", True)
        c, hatchet = client
        _post(c, _pr_payload(action="opened", repo="amerenda/sazed", pr_number=99))
        push_payload = hatchet.event.push.call_args[0][1]
        assert push_payload["repo"] == "amerenda/sazed"
        assert push_payload["pr_number"] == "99"
        assert "github.com/amerenda/sazed/pull/99" in push_payload["pr_url"]
        assert ".diff" in push_payload["diff_url"]
        assert push_payload["author"] == "human-dev"

    def test_pr_number_is_string(self, client, monkeypatch):
        monkeypatch.setattr("webhooks.github._PR_REVIEW_ENABLED", True)
        c, hatchet = client
        _post(c, _pr_payload(action="opened", pr_number=123))
        push_payload = hatchet.event.push.call_args[0][1]
        assert isinstance(push_payload["pr_number"], str)
        assert push_payload["pr_number"] == "123"

    def test_valid_signature_accepted(self, client, monkeypatch):
        c, hatchet = client
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "ghs")
        body_dict = _pr_payload(action="opened")
        body = json.dumps(body_dict).encode()
        sig = _sig(body, "ghs")
        resp = c.post(
            "/webhooks/github",
            content=body,
            headers={"Content-Type": "application/json", "X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig},
        )
        assert resp.status_code == 200

    def test_invalid_signature_rejected(self, client, monkeypatch):
        c, _ = client
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "ghs")
        resp = c.post(
            "/webhooks/github",
            content=b'{}',
            headers={"Content-Type": "application/json", "X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=badhash"},
        )
        assert resp.status_code == 401

    def test_missing_signature_with_secret_rejected(self, client, monkeypatch):
        c, _ = client
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "ghs")
        resp = c.post(
            "/webhooks/github",
            content=b'{}',
            headers={"Content-Type": "application/json", "X-GitHub-Event": "pull_request"},
        )
        assert resp.status_code == 401
