"""GitHub App authentication: JWT → installation token."""
import time
import os
import httpx
import jwt  # PyJWT


def _make_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": app_id}
    return jwt.encode(payload, private_key, algorithm="RS256")


def _get_installation_token(app_id: str, private_key: str, installation_id: str) -> str:
    jwt_token = _make_jwt(app_id, private_key)
    resp = httpx.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def get_installation_token() -> str:
    """Returns a short-lived GitHub installation token for the coder app."""
    return _get_installation_token(
        app_id=os.environ["CODER_APP_ID"],
        private_key=os.environ["CODER_APP_PRIVATE_KEY"].replace("\\n", "\n"),
        installation_id=os.environ["CODER_APP_INSTALLATION_ID"],
    )


def get_reviewer_installation_token() -> str:
    """Returns a short-lived GitHub installation token for the reviewer app."""
    return _get_installation_token(
        app_id=os.environ["REVIEWER_APP_ID"],
        private_key=os.environ["REVIEWER_APP_PRIVATE_KEY"].replace("\\n", "\n"),
        installation_id=os.environ["REVIEWER_APP_INSTALLATION_ID"],
    )
