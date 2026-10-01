"""Host-side worker transport: wheel resolution and the WorkerClient."""

from lantern.worker.client import WorkerClient
from lantern.worker.wheel import resolve_worker_wheel

__all__ = ["WorkerClient", "resolve_worker_wheel"]
