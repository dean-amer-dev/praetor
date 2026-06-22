"""Langfuse integration: prompt management and tracing.

If LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY env vars are not set, all exports
become no-ops so workers start normally without Langfuse configured.
"""
import logging
import os

logger = logging.getLogger(__name__)

_ENABLED = all(
    os.environ.get(k)
    for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
)

if _ENABLED:
    from langfuse import Langfuse
    from langfuse.decorators import langfuse_context, observe  # type: ignore[assignment]

    _client = Langfuse(
        host=os.environ["LANGFUSE_HOST"],
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    )

else:
    import functools

    def observe():  # type: ignore[misc]
        def decorator(fn):
            return fn
        return decorator

    class _NullContext:
        def update_current_trace(self, **kwargs): ...

    langfuse_context = _NullContext()  # type: ignore[assignment]
    _client = None


def get_system_prompt(name: str, fallback: str = "") -> str:
    """Return the production-labeled prompt from Langfuse, or fallback if unavailable."""
    if _client is None:
        return fallback
    try:
        return _client.get_prompt(name).compile()
    except Exception:
        return fallback


def create_prompt(name: str, prompt_text: str) -> bool:
    """Create or update a production-labeled prompt in Langfuse. Returns True on success."""
    if _client is None:
        return False
    try:
        _client.create_prompt(name=name, prompt=prompt_text, labels=["production"])
        return True
    except Exception as exc:
        logger.warning("langfuse create_prompt(%s) failed: %s", name, exc)
        return False
