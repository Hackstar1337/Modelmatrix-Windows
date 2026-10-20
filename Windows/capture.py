"""Per-profile Windows capture listeners.

A capture listener owns only its HTTP server.  The UsageRelay's SQLite store and
in-flight accounting remain owned by the controller's main relay, so stopping a
profile listener can never close the shared database twice.
"""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from typing import Any

try:
    from modelmatrix_agent import UsageRelayHandler, UsageTarget
except ImportError:  # package import
    from RemoteAgent.modelmatrix_agent import UsageRelayHandler, UsageTarget  # type: ignore


class _CaptureOwner:
    def __init__(self, host: str, port: int, target: UsageTarget, usage: Any) -> None:
        self.host, self.port, self.target, self.store = host, port, target, usage.store
        self.inflight = usage.inflight
        self._inflight_lock = usage._inflight_lock
        self.server: ThreadingHTTPServer | None = None
        self.error: str | None = None

    def resolve(self, _model: str | None, _path: str) -> UsageTarget:
        return self.target

    def begin(self, entry: dict[str, Any]) -> None:
        with self._inflight_lock:
            self.inflight[entry["id"]] = entry

    def end(self, request_id: str) -> None:
        with self._inflight_lock:
            self.inflight.pop(request_id, None)

    def start(self) -> bool:
        owner = self
        class Handler(UsageRelayHandler):
            pass
        Handler.relay = owner
        try:
            self.server = ThreadingHTTPServer((self.host.strip("[]"), self.port), Handler)
            self.server.daemon_threads = True
            threading.Thread(target=self.server.serve_forever, name=f"capture-{self.port}", daemon=True).start()
            return True
        except OSError as error:
            self.error = f"capture port {self.port} unavailable: {error.strerror or error}"
            self.server = None
            return False

    @property
    def listening(self) -> bool:
        return self.server is not None

    def stop(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.server_close()


def start_capture(host: str, public_port: int, internal_port: int, profile: str,
                  runtime: str, models: list[str], usage: Any) -> _CaptureOwner | None:
    target = UsageTarget(profile, runtime, "127.0.0.1", internal_port, models, True, True)
    listener = _CaptureOwner(host, public_port, target, usage)
    return listener if listener.start() else None
