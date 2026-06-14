"""Unit tests for webhooks/vikunja.py — label routing and signature verification."""
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from webhooks.vikunja import _verify_signature, LABEL_RESEARCH, LABEL_GO, LABEL_PLAN_ONLY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sig(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _task_payload(task_id: int = 42, label_ids: list[int] | None = None, event_type: str = "task.updated", title: str = "Test task", description: str = "desc") -> dict:
    labels = [{"id": lid, "title": f"label-{lid}"} for lid in (label_ids or [])]
    return {
        "event_type": event_type,
        "data": {
            "task": {
                "id": task_id,
                "title": title,
                "description": description,
                "labels": labels,
            }
        },
    }


# ---------------------------------------------------------------------------
# Signature verification (pure function tests — no HTTP)
# ---------------------------------------------------------------------------

class TestVerifySignature:
    def test_valid_signature_accepted(self, monkeypatch):
        monkeypatch.setenv("VIKUNJA_WEBHOOK_SECRET", "mysecret")
        body = b'{"hello": "world"}'
        header = _sig(body, "mysecret")
        _verify_signature(body, header)  # must not raise

    def test_invalid_signature_raises_401(self, monkeypatch):
        monkeypatch.setenv("VIKUNJA_WEBHOOK_SECRET", "mysecret")
        body = b'{"hello": "world"}'
        with pytest.raises(HTTPException) as exc:
            _verify_signature(body, "sha256=deadbeef")
        assert exc.value.status_code == 401

    def test_missing_header_with_secret_passes(self, monkeypatch):
        """Vikunja doesn't implement HMAC signing — missing header is allowed."""
        monkeypatch.setenv("VIKUNJA_WEBHOOK_SECRET", "mysecret")
        _verify_signature(b"body", None)  # must not raise

    def test_no_secret_configured_passes(self, monkeypatch):
        monkeypatch.delenv("VIKUNJA_WEBHOOK_SECRET", raising=False)
        _verify_signature(b"body", "sha256=anything")  # must not raise


# ---------------------------------------------------------------------------
# Webhook routing via TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def client(mock_hatchet):
    """FastAPI TestClient with Hatchet and Vikunja startup registration stubbed."""
    with patch("webhooks.vikunja.register_webhook_on_startup", new=AsyncMock()):
        from webhooks.app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c, mock_hatchet


def _post(client, payload: dict, secret: str | None = None) -> "httpx.Response":
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Vikunja-Signature"] = _sig(body, secret)
    return client.post("/webhooks/vikunja", content=body, headers=headers)


class TestLabelRouting:
    def test_research_label_only_dispatches_agent_research(self, client):
        c, hatchet = client
        payload = _task_payload(label_ids=[LABEL_RESEARCH])
        resp = _post(c, payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["dispatched"] == ["agent:research"]
        hatchet.event.push.assert_called_once()
        event_name = hatchet.event.push.call_args[0][0]
        assert event_name == "agent:research"

    def test_go_label_only_dispatches_agent_code(self, client):
        c, hatchet = client
        payload = _task_payload(label_ids=[LABEL_GO])
        resp = _post(c, payload)
        assert resp.status_code == 200
        assert resp.json()["dispatched"] == ["agent:code"]

    def test_both_labels_dispatches_pipeline(self, client):
        c, hatchet = client
        payload = _task_payload(label_ids=[LABEL_RESEARCH, LABEL_GO])
        resp = _post(c, payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["dispatched"] == ["pipeline:research_code"]
        # Must NOT push individual events
        assert hatchet.event.push.call_count == 1
        event_name = hatchet.event.push.call_args[0][0]
        assert event_name == "pipeline:research_code"

    def test_plan_only_label_dispatches_nothing(self, client):
        c, hatchet = client
        payload = _task_payload(label_ids=[LABEL_PLAN_ONLY])
        resp = _post(c, payload)
        assert resp.status_code == 200
        assert resp.json()["dispatched"] == []
        hatchet.event.push.assert_not_called()

    def test_unrelated_label_dispatches_nothing(self, client):
        c, hatchet = client
        payload = _task_payload(label_ids=[99, 100])
        resp = _post(c, payload)
        assert resp.status_code == 200
        assert resp.json()["dispatched"] == []

    def test_no_labels_dispatches_nothing(self, client):
        c, hatchet = client
        payload = _task_payload(label_ids=[])
        resp = _post(c, payload)
        assert resp.status_code == 200
        assert resp.json()["dispatched"] == []

    def test_null_labels_field_no_crash(self, client):
        c, hatchet = client
        payload = {
            "event_type": "task.updated",
            "data": {"task": {"id": 1, "title": "t", "labels": None}},
        }
        resp = _post(c, payload)
        assert resp.status_code == 200

    def test_task_created_event_handled(self, client):
        c, hatchet = client
        payload = _task_payload(label_ids=[LABEL_RESEARCH], event_type="task.created")
        resp = _post(c, payload)
        assert resp.status_code == 200
        assert "agent:research" in resp.json()["dispatched"]

    def test_ignored_event_type(self, client):
        c, hatchet = client
        payload = _task_payload(label_ids=[LABEL_RESEARCH], event_type="comment.created")
        resp = _post(c, payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        hatchet.event.push.assert_not_called()

    def test_missing_task_id(self, client):
        c, hatchet = client
        payload = {"event_type": "task.updated", "data": {"task": {"labels": [{"id": LABEL_RESEARCH}]}}}
        resp = _post(c, payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "no task id"

    def test_payload_fields_forwarded(self, client):
        c, hatchet = client
        payload = _task_payload(task_id=99, label_ids=[LABEL_RESEARCH], title="My Title", description="My Desc")
        resp = _post(c, payload)
        assert resp.status_code == 200
        push_payload = hatchet.event.push.call_args[0][1]
        assert push_payload["task_id"] == 99
        assert push_payload["task_title"] == "My Title"
        assert push_payload["task_description"] == "My Desc"

    def test_pipeline_includes_metadata(self, client):
        c, hatchet = client
        payload = _task_payload(task_id=7, label_ids=[LABEL_RESEARCH, LABEL_GO])
        resp = _post(c, payload)
        assert resp.status_code == 200
        meta = hatchet.event.push.call_args[1].get("additional_metadata", {})
        assert meta.get("vikunja_task_id") == "7"
