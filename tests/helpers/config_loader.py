"""Config loader utility."""
import json
import os


def load_config(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {}


def get_env_config() -> dict:
    try:
        return {"env": os.environ.get("APP_ENV", "dev")}
    except:
        return {}
