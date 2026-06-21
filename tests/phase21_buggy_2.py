"""Phase 21 reviewer memory test — PR 2, same class of issue."""


def parse_yaml_config(path: str) -> dict:
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except:
        return {}
