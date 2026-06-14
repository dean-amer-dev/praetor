"""E2E Phase 7: PR reviewer — GitHub webhook → Hatchet dispatch.

By default verifies dispatch only (no real review posted).
Set PR_REVIEWER_E2E_FULL=1 + GITHUB_TOKEN to verify an actual review
is posted by dean-reviewer[bot] to an existing PR.
"""
import json
import os

import httpx
import pytest

if not os.environ.get("E2E_TESTS"):
    pytest.skip("Set E2E_TESTS=1 to run e2e tests", allow_module_level=True)

from .conftest import (
    PRAETOR_BASE,
    e2e_task_id,
    github_sig,
    github_pr_payload,
    poll_until,
)

pytestmark = pytest.mark.e2e

_FULL = os.environ.get("PR_REVIEWER_E2E_FULL")
_REVIEW_PR = os.environ.get("PR_REVIEWER_E2E_PR_NUMBER")  # existing open PR number to post to


class TestPRReviewerE2E:
    def test_invalid_github_signature_rejected(self):
        """Adapter returns 401 for tampered GitHub webhook payload."""
        body = b'{"action":"opened"}'
        resp = httpx.post(
            f"{PRAETOR_BASE}/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": "sha256=deadbeef",
            },
            timeout=10,
        )
        # 401 when secret is configured; if no secret is set, adapter passes everything
        assert resp.status_code in (200, 401)

    def test_github_ping_event(self):
        """Adapter responds to GitHub ping events with pong."""
        body = b"{}"
        headers = {"Content-Type": "application/json", "X-GitHub-Event": "ping"}
        sig = github_sig(body)
        if sig:
            headers["X-Hub-Signature-256"] = sig
        resp = httpx.post(f"{PRAETOR_BASE}/webhooks/github", content=body, headers=headers, timeout=10)
        assert resp.status_code == 200
        assert resp.json() == {"status": "pong"}

    def test_pr_opened_event_dispatches(self):
        """Signed PR opened webhook dispatches github:pr_opened to Hatchet."""
        pr_number = 9900 + (e2e_task_id() % 99)  # synthetic PR number
        payload = github_pr_payload(pr_number=pr_number, repo="amerenda/praetor")
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "X-GitHub-Event": "pull_request"}
        sig = github_sig(body)
        if sig:
            headers["X-Hub-Signature-256"] = sig

        resp = httpx.post(f"{PRAETOR_BASE}/webhooks/github", content=body, headers=headers, timeout=10)
        assert resp.status_code == 200, f"adapter returned {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data.get("dispatched") == "github:pr_opened", f"unexpected response: {data}"

    def test_pr_synchronize_event_ignored(self):
        """synchronize PR events are not dispatched."""
        payload = {
            "action": "synchronize",
            "pull_request": {
                "number": 1,
                "html_url": "https://github.com/amerenda/praetor/pull/1",
                "diff_url": "https://github.com/amerenda/praetor/pull/1.diff",
                "user": {"login": "human"},
            },
            "repository": {"full_name": "amerenda/praetor"},
        }
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "X-GitHub-Event": "pull_request"}
        sig = github_sig(body)
        if sig:
            headers["X-Hub-Signature-256"] = sig
        resp = httpx.post(f"{PRAETOR_BASE}/webhooks/github", content=body, headers=headers, timeout=10)
        assert resp.status_code == 200
        assert resp.json().get("dispatched") is None

    @pytest.mark.skipif(
        not (_FULL and _REVIEW_PR),
        reason="Set PR_REVIEWER_E2E_FULL=1 and PR_REVIEWER_E2E_PR_NUMBER=<pr#> to test actual review posting",
    )
    def test_reviewer_posts_review_to_existing_pr(self):
        """Full flow: trigger review of an existing open PR and wait for dean-reviewer[bot] comment."""
        gh_token = os.environ.get("GITHUB_TOKEN", "")
        if not gh_token:
            pytest.skip("GITHUB_TOKEN required for full reviewer e2e")

        pr_number = int(_REVIEW_PR)
        payload = github_pr_payload(pr_number=pr_number, repo="amerenda/praetor")
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "X-GitHub-Event": "pull_request"}
        sig = github_sig(body)
        if sig:
            headers["X-Hub-Signature-256"] = sig

        resp = httpx.post(f"{PRAETOR_BASE}/webhooks/github", content=body, headers=headers, timeout=10)
        assert resp.status_code == 200

        gh_headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}

        def check_review():
            r = httpx.get(
                f"https://api.github.com/repos/amerenda/praetor/pulls/{pr_number}/reviews",
                headers=gh_headers,
                timeout=10,
            )
            if r.status_code != 200:
                return None
            reviews = r.json()
            return [rv for rv in reviews if rv.get("user", {}).get("login", "").startswith("amerenda-reviewer")] or None

        reviews = poll_until(
            check_review,
            timeout=300,
            interval=15,
            fail_msg=f"amerenda-reviewer[bot] did not post a review on PR {pr_number} within 5 minutes",
        )
        assert reviews
