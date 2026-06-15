"""Langfuse integration: prompt management and tracing.

If LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY env vars are not set, all exports
become no-ops so workers start normally without Langfuse configured.
"""
import os

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
