"""File parser utility."""
import json


def parse_json_file(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {}


def parse_lines(path: str) -> list:
    try:
        with open(path) as f:
            return f.readlines()
    except:
        return []
