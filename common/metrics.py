"""Shared Prometheus metrics for all Praetor agent workers.

Starts a lightweight HTTP server on METRICS_PORT (default 9095) in a background
thread so each worker pod can be scraped by Prometheus without any extra sidecars.
"""
import os
import threading

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

_REGISTRY = CollectorRegistry(auto_describe=True)

task_invocations = Counter(
    "praetor_task_invocations_total",
    "Total number of tasks dispatched to this agent",
    ["agent", "status"],
    registry=_REGISTRY,
)

task_active = Gauge(
    "praetor_task_active",
    "Number of tasks currently running in this agent",
    ["agent"],
    registry=_REGISTRY,
)

task_duration = Histogram(
    "praetor_task_duration_seconds",
    "End-to-end wall-clock time for a completed task",
    ["agent"],
    buckets=[30, 60, 120, 300, 600, 900, 1200],
    registry=_REGISTRY,
)

_started = False
_lock = threading.Lock()


def start_metrics_server(agent_name: str) -> None:
    """Start the Prometheus HTTP server once per process."""
    global _started
    with _lock:
        if _started:
            return
        port = int(os.environ.get("METRICS_PORT", "9095"))
        start_http_server(port, registry=_REGISTRY)
        # Pre-create label combinations so they appear in /metrics from the start.
        task_invocations.labels(agent=agent_name, status="success")
        task_invocations.labels(agent=agent_name, status="error")
        task_active.labels(agent=agent_name)
        task_duration.labels(agent=agent_name)
        _started = True
