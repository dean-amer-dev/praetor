"""GitHub App authentication: JWT → installation token."""
import time
import os
import httpx
import jwt  # PyJWT

_GH_API = "https://api.github.com"
_GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _make_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": app_id}
    return jwt.encode(payload, private_key, algorithm="RS256")


def _get_installation_token(app_id: str, private_key: str, installation_id: str) -> str:
    jwt_token = _make_jwt(app_id, private_key)
    resp = httpx.post(
        f"{_GH_API}/app/installations/{installation_id}/access_tokens",
        headers={**_GH_HEADERS, "Authorization": f"Bearer {jwt_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def _find_installation_id(app_id: str, private_key: str, owner_repo: str) -> str:
    """Look up the installation ID that covers owner_repo for this app.

    Tries the repo-specific endpoint first (fast), then falls back to listing
    all installations and matching by account login (handles org-level installs
    that aren't returned by the repo endpoint).
    """
    owner = owner_repo.split("/")[0]
    jwt_token = _make_jwt(app_id, private_key)
    auth = {"Authorization": f"Bearer {jwt_token}", **_GH_HEADERS}

    # Fast path: repo has the app installed directly
    resp = httpx.get(f"{_GH_API}/repos/{owner_repo}/installation", headers=auth, timeout=10)
    if resp.status_code == 200:
        return str(resp.json()["id"])

    # Fallback: list all installations and match by account
    page = 1
    while True:
        resp = httpx.get(
            f"{_GH_API}/app/installations",
            headers=auth,
            params={"per_page": 100, "page": page},
            timeout=10,
        )
        resp.raise_for_status()
        installs = resp.json()
        if not installs:
            break
        for inst in installs:
            if inst.get("account", {}).get("login", "").lower() == owner.lower():
                return str(inst["id"])
        page += 1

    raise ValueError(
        f"praetor-coder GitHub App is not installed on '{owner}'. "
        f"Install it at https://github.com/apps/praetor-coder and grant access to {owner_repo}."
    )


def get_installation_token(repo: str | None = None) -> str:
    """Returns a short-lived GitHub installation token for the coder app.

    If repo is given (format 'owner/repo'), dynamically discovers the correct
    installation — supports any org or user account where the app is installed.
    Falls back to CODER_APP_INSTALLATION_ID when repo is not specified.
    """
    app_id = os.environ["CODER_APP_ID"]
    private_key = os.environ["CODER_APP_PRIVATE_KEY"].replace("\\n", "\n")
    if repo:
        installation_id = _find_installation_id(app_id, private_key, repo)
    else:
        installation_id = os.environ["CODER_APP_INSTALLATION_ID"]
    return _get_installation_token(app_id, private_key, installation_id)


def get_reviewer_installation_token() -> str:
    """Returns a short-lived GitHub installation token for the reviewer app."""
    return _get_installation_token(
        app_id=os.environ["REVIEWER_APP_ID"],
        private_key=os.environ["REVIEWER_APP_PRIVATE_KEY"].replace("\\n", "\n"),
        installation_id=os.environ["REVIEWER_APP_INSTALLATION_ID"],
    )
