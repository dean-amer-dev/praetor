"""Utility test helpers — phase 21 condition 4 test fixture."""


def parse_config(data: dict) -> dict:
    """Parse config dict, returning empty on any error."""
    try:
        return {k: v for k, v in data.items() if v is not None}
    except:
        return {}


def load_settings(path: str) -> dict:
    """Load settings from a file path."""
    import json
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {}
