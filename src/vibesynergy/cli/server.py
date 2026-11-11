"""Signal-aware Uvicorn server for the combined VibeSynergy service."""

from __future__ import annotations

import contextlib
import os
import signal
import threading
from collections.abc import Generator
from types import FrameType

import uvicorn
from uvicorn.server import HANDLED_SIGNALS


class ServiceServer(uvicorn.Server):
    EXIT_DEADLINE = 14.0

    def __init__(self, config: uvicorn.Config, stop: threading.Event) -> None:
        super().__init__(config)
        self._stop = stop
        self.portal_server: uvicorn.Server | None = None
        self._exit_watchdog_started = False

    def _force_exit(self) -> None:
        threading.Event().wait(self.EXIT_DEADLINE)
        # The signal handler starts this only after setting stop. A synchronous AnyIO
        # worker can outlive Uvicorn and Python will otherwise wait for it forever.
        os._exit(0)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        # Stop queue claims and scheduling as soon as shutdown begins, rather than after
        # Uvicorn has waited for open HTTP connections and application shutdown.
        self._stop.set()
        if not self._exit_watchdog_started:
            self._exit_watchdog_started = True
            threading.Thread(target=self._force_exit, name="shutdown-deadline", daemon=True).start()
        if self.portal_server is not None:
            self.portal_server.should_exit = True
        super().handle_exit(sig, frame)

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original = {sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS}
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)
        # Uvicorn normally re-raises the signal here. This command has completed a
        # controlled shutdown, so return success to both the entry point and uv run.
