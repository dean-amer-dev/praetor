"""Unit tests for common/github_app.py — JWT construction and token exchange."""
import time
from unittest.mock import MagicMock, patch

import pytest

# We need a real RSA key pair for JWT signing tests
try:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


@pytest.fixture(scope="module")
def rsa_keypair():
    if not HAS_CRYPTO:
        pytest.skip("cryptography library not available")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return private_pem


class TestMakeJwt:
    def test_jwt_has_correct_claims(self, rsa_keypair):
        import jwt as pyjwt
        from common.github_app import _make_jwt
        token = _make_jwt("my-app-id", rsa_keypair)
        # Decode without verification to inspect claims
        claims = pyjwt.decode(token, options={"verify_signature": False})
        assert claims["iss"] == "my-app-id"
        now = int(time.time())
        assert abs(claims["iat"] - (now - 60)) < 5
        assert abs(claims["exp"] - (now + 540)) < 5

    def test_jwt_uses_rs256(self, rsa_keypair):
        import jwt as pyjwt
        from common.github_app import _make_jwt
        token = _make_jwt("app-id", rsa_keypair)
        header = pyjwt.get_unverified_header(token)
        assert header["alg"] == "RS256"

    def test_jwt_is_string(self, rsa_keypair):
        from common.github_app import _make_jwt
        token = _make_jwt("app-id", rsa_keypair)
        assert isinstance(token, str)


class TestGetInstallationToken:
    def test_calls_github_api(self, rsa_keypair):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"token": "ghs_test_token"}
        mock_resp.raise_for_status = MagicMock()

        with patch("common.github_app.httpx.post", return_value=mock_resp) as mock_post:
            from common.github_app import _get_installation_token
            token = _get_installation_token("app-123", rsa_keypair, "install-456")

        assert token == "ghs_test_token"
        assert mock_post.called
        url = mock_post.call_args[0][0]
        assert "install-456" in url
        assert "access_tokens" in url

    def test_raises_on_http_error(self, rsa_keypair):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("401 Unauthorized")

        with patch("common.github_app.httpx.post", return_value=mock_resp):
            from common.github_app import _get_installation_token
            with pytest.raises(Exception, match="401"):
                _get_installation_token("app-id", rsa_keypair, "install-id")

    def test_newline_escape_in_key_handled(self, monkeypatch, rsa_keypair):
        """Keys stored in BWS have \\n instead of real newlines — must be converted."""
        escaped_key = rsa_keypair.replace("\n", "\\n")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"token": "tok"}
        mock_resp.raise_for_status = MagicMock()

        monkeypatch.setenv("CODER_APP_ID", "123")
        monkeypatch.setenv("CODER_APP_PRIVATE_KEY", escaped_key)
        monkeypatch.setenv("CODER_APP_INSTALLATION_ID", "456")

        with patch("common.github_app.httpx.post", return_value=mock_resp):
            from common.github_app import get_installation_token
            token = get_installation_token()
        assert token == "tok"


class TestGetReviewerToken:
    def test_uses_reviewer_env_vars(self, rsa_keypair, monkeypatch):
        escaped_key = rsa_keypair.replace("\n", "\\n")
        monkeypatch.setenv("REVIEWER_APP_ID", "reviewer-app-id")
        monkeypatch.setenv("REVIEWER_APP_PRIVATE_KEY", escaped_key)
        monkeypatch.setenv("REVIEWER_APP_INSTALLATION_ID", "reviewer-install-id")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"token": "reviewer-tok"}
        mock_resp.raise_for_status = MagicMock()

        with patch("common.github_app.httpx.post", return_value=mock_resp) as mock_post:
            from common.github_app import get_reviewer_installation_token
            token = get_reviewer_installation_token()

        assert token == "reviewer-tok"
        url = mock_post.call_args[0][0]
        assert "reviewer-install-id" in url
