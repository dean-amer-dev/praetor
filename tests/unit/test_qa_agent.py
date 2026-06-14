"""Unit tests for agents/qa/agent.py — http_check, playwright_check, post_qa_result."""
from unittest.mock import MagicMock, patch

import httpx
import pytest


class TestHttpCheck:
    def test_success_200(self):
        from agents.qa.agent import http_check
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        with patch("agents.qa.agent.httpx.get", return_value=mock_resp):
            result = http_check("http://example.com")
        assert result["ok"] is True
        assert result["status"] == 200
        assert result["url"] == "http://example.com"

    def test_non_2xx_is_not_ok(self):
        from agents.qa.agent import http_check
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.is_success = False
        with patch("agents.qa.agent.httpx.get", return_value=mock_resp):
            result = http_check("http://example.com/missing")
        assert result["ok"] is False
        assert result["status"] == 404

    def test_connection_error(self):
        from agents.qa.agent import http_check
        with patch("agents.qa.agent.httpx.get", side_effect=httpx.ConnectError("refused")):
            result = http_check("http://down.invalid")
        assert result["ok"] is False
        assert "error" in result

    def test_timeout_error(self):
        from agents.qa.agent import http_check
        with patch("agents.qa.agent.httpx.get", side_effect=httpx.TimeoutException("timed out")):
            result = http_check("http://slow.invalid")
        assert result["ok"] is False
        assert "error" in result

    def test_url_in_result(self):
        from agents.qa.agent import http_check
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        with patch("agents.qa.agent.httpx.get", return_value=mock_resp):
            result = http_check("http://specific-url.test")
        assert result["url"] == "http://specific-url.test"


class TestPlaywrightCheck:
    def test_import_error_returns_ok_false(self):
        """Playwright may not be installed in the test env — should not crash."""
        from agents.qa.agent import playwright_check
        # If playwright isn't installed, we expect ok=False with an error message
        result = playwright_check("http://example.com")
        # Either it worked (playwright installed) or it returned ok=False
        if not result["ok"]:
            assert "error" in result
        # url is always set
        assert result["url"] == "http://example.com"

    def test_page_error_captured(self):
        """JS errors captured via page.on('pageerror', ...) appear in console_errors."""
        playwright_mod = MagicMock()
        mock_browser = MagicMock()
        mock_page = MagicMock()
        mock_page.title.return_value = "Test Page"

        # Simulate a page error callback being registered
        def mock_on(event, callback):
            if event == "pageerror":
                callback(Exception("ReferenceError: foo is not defined"))

        mock_page.on.side_effect = mock_on
        mock_browser.new_page.return_value = mock_page
        playwright_mod.chromium.launch.return_value = mock_browser

        # sync_playwright is imported inside the function body, patch at the source module
        with patch("playwright.sync_api.sync_playwright") as mock_playwright:
            mock_playwright.return_value.__enter__.return_value = playwright_mod
            from agents.qa.agent import playwright_check
            result = playwright_check("http://example.com")

        # Should have captured the error and not be ok
        assert result["ok"] is False
        assert len(result["console_errors"]) > 0


class TestPostQaResult:
    def test_passed_format(self):
        from agents.qa.agent import post_qa_result
        result = post_qa_result("amerenda/ecdysis", 42, "all good", True)
        assert "PASSED" in result
        assert "amerenda/ecdysis" in result

    def test_failed_format(self):
        from agents.qa.agent import post_qa_result
        result = post_qa_result("amerenda/ecdysis", 42, "broken", False)
        assert "FAILED" in result

    def test_pr_number_in_result(self):
        from agents.qa.agent import post_qa_result
        result = post_qa_result("repo", 99, "summary", True)
        assert "PR#99" in result

    def test_no_pr_number(self):
        from agents.qa.agent import post_qa_result
        result = post_qa_result("repo", None, "summary", True)
        assert "PR#" not in result

    def test_summary_in_result(self):
        from agents.qa.agent import post_qa_result
        result = post_qa_result("repo", None, "my detailed summary", True)
        assert "my detailed summary" in result
