from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Windows"))
sys.path.insert(0, str(ROOT / "RemoteAgent"))

import capture  # noqa: E402
import controller  # noqa: E402
from modelmatrix_agent import AgentConfiguration  # noqa: E402


class _FakeCaptureServer:
    def __init__(self) -> None:
        self.shutdown_calls = 0
        self.close_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def server_close(self) -> None:
        self.close_calls += 1


class ControllerRouteRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir()
        (self.profiles / "fixture.env").write_text(
            "DISPLAY_NAME=Fixture\nRUNTIME=external\nHOST=127.0.0.1\nPORT=8080\nREQUEST_MODEL=fixture-model\n",
            encoding="utf-8",
        )
        config = AgentConfiguration(
            root=self.root,
            profiles_dir=self.profiles,
            host="127.0.0.1",
            port=0,
            auth_token="t" * 32,
        )
        # A task-owned relay listener exercises the real usage store without
        # contacting the user's default Ollama or any existing controller.
        config.relay_port = self._free_port()
        self.service = controller.LocalService(config)
        self.service.bootstrap()

    def tearDown(self) -> None:
        self.service.stop_all_managed()
        self.temp.cleanup()

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def route(self, method: str, path: str, body=None, query=None):
        result = self.service.module_route(method, path, query or {}, body or {})
        self.assertIsNotNone(result, path)
        return result

    def record(self, **changes):
        row = dict(
            started_at=__import__("time").time(),
            client_id="pc-client",
            client_label="fixture client",
            model="fixture-model",
            model_requested="fixture-model",
            profile="fixture",
            runtime="external",
            tier="local",
            host="local",
            endpoint="/v1/chat/completions",
            method="POST",
            status=200,
            stream=0,
            prompt_tokens=2,
            completion_tokens=3,
            duration_ms=50,
            ttft_ms=5,
            tokens_per_second=60,
        )
        row.update(changes)
        self.service.usage.store.record(row)

    def test_route_surface_profile_usage_optimization_and_validation(self):
        for method, path, body, query in [
            ("GET", "/api/profiles", None, None),
            ("GET", "/api/profiles/runtimes", None, None),
            ("GET", "/api/profiles/document", None, {"profile": "fixture"}),
            ("GET", "/api/usage/overview", None, {"range": "24h"}),
            ("GET", "/api/usage/summary", None, {"range": "24h"}),
            ("GET", "/api/usage/timeseries", None, {"range": "24h"}),
            ("GET", "/api/usage/models", None, {"range": "24h"}),
            ("GET", "/api/usage/clients", None, {"range": "24h"}),
            ("GET", "/api/usage/requests", None, {"range": "all"}),
            ("GET", "/api/usage/live", None, None),
            ("GET", "/api/usage/settings", None, None),
            ("GET", "/api/optimize/plan", None, None),
            ("GET", "/api/dashboard/session/info", None, None),
            ("GET", "/api/dashboard/info", None, None),
        ]:
            status, payload = self.route(method, path, body, query)
            self.assertEqual(status, 200, path)
            self.assertIsInstance(payload, dict, path)

        status, payload = self.route("POST", "/api/profiles/create", {"profile": "Created", "values": {"RUNTIME": "external"}})
        self.assertEqual(status, 200)
        self.assertTrue(payload["created"])
        status, payload = self.route("POST", "/api/profiles/duplicate", {"profile": "Created", "new_profile": "Copied"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["duplicated_from"], "Created")
        status, payload = self.route(
            "POST", "/api/profiles/save",
            {"profile": "Renamed", "original_profile": "Copied", "text": "RUNTIME=external\nREQUEST_MODEL=changed\n"},
        )
        self.assertEqual(status, 200)
        self.assertTrue((self.profiles / "Renamed.env").is_file())
        status, payload = self.route("POST", "/api/profiles/delete", {"profile": "Renamed"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["deleted"])

        status, payload = self.route("POST", "/api/usage/settings", {"retention_days": 30, "enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        with self.assertRaises(controller.UsageError):
            self.route("POST", "/api/usage/settings", {"enabled": "false"})
        with self.assertRaises(controller.UsageError):
            self.route("POST", "/api/usage/settings", {"retention_days": True})
        with self.assertRaises(controller.UsageError):
            self.route("POST", "/api/usage/reset", {})
        status, payload = self.route("POST", "/api/usage/reset", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["reset"])
        status, payload = self.route("POST", "/api/optimize/purge", {})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "unsupported_capability")

        for invalid in ("CON", "name.", "name ", "../escape", "x/y"):
            with self.assertRaises(controller.UsageError):
                controller._profile_name(invalid)

    def test_usage_search_export_previous_period_and_settings_are_real(self):
        import time

        self.record()
        self.record(started_at=time.time() - 90000, tier="cloud", completion_tokens=9)
        status, payload = self.route("POST", "/api/usage/clients/rename", {"client_id": "pc-client", "name": "Named PC"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["renamed"])
        status, summary = self.route("GET", "/api/usage/summary", query={"range": "24h", "host": "local"})
        self.assertEqual(status, 200)
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["previous"]["requests"], 1)
        status, requests = self.route("GET", "/api/usage/requests", query={"range": "all", "q": "Named PC", "limit": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(requests["total"], 2)
        self.assertEqual(requests["requests"][0]["client_name"], "Named PC")
        status, data, headers = self.service.usage_local.csv({"range": "all", "q": "Named PC"})
        self.assertEqual(status, 200)
        self.assertIn(b"Named PC", data)
        self.assertIn("text/csv", headers["Content-Type"])

    def test_dashboard_auth_and_csrf_helpers_scope_browser_sessions(self):
        manager = controller.SessionManager("t" * 32)
        session = manager.issue()
        self.assertTrue(manager.token_valid("t" * 32))
        self.assertFalse(manager.token_valid("wrong"))
        self.assertTrue(manager.valid(session))
        manager.revoke(session)
        self.assertFalse(manager.valid(session))
        session = manager.issue()
        handler = object.__new__(controller.DashboardHandler)
        handler.server = SimpleNamespace(
            auth_token="t" * 32,
            session_manager=manager,
            server_address=("127.0.0.1", 12345),
            server_port=12345,
        )
        handler.headers = {"Host": "127.0.0.1:12345", "Cookie": f"{controller.SESSION_COOKIE}={session}"}
        self.assertTrue(handler._host_allowed())
        self.assertTrue(handler._csrf_ok("POST", "/api/start"))
        handler.headers = {"Host": "127.0.0.1:12345", "Origin": "https://evil.example", "Cookie": f"{controller.SESSION_COOKIE}={session}"}
        self.assertFalse(handler._csrf_ok("POST", "/api/start"))
        handler.headers = {"Host": "attacker.example"}
        self.assertFalse(handler._host_allowed())
        handler.headers = {"Host": "127.0.0.1:12345", "Authorization": "Bearer " + "t" * 32}
        self.assertTrue(handler._csrf_ok("POST", "/api/start"))
        handler.headers = {"Host": "127.0.0.1:12345", "Authorization": "Bearer wrong"}
        self.assertFalse(handler._csrf_ok("POST", "/api/start"))


class CaptureAndShutdownRegressionTests(unittest.TestCase):
    def test_capture_owner_tracks_inflight_and_releases_server(self):
        class Store:
            pass

        usage = SimpleNamespace(store=Store(), inflight={}, _inflight_lock=threading.Lock())
        owner = capture._CaptureOwner("127.0.0.1", 43123, SimpleNamespace(profile="fixture"), usage)
        entry = {"id": "request-1", "profile": "fixture"}
        owner.begin(entry)
        self.assertEqual(owner.inflight["request-1"], entry)
        owner.end("request-1")
        self.assertNotIn("request-1", owner.inflight)
        fake = _FakeCaptureServer()
        owner.server = fake
        owner.stop()
        owner.stop()
        self.assertEqual(fake.shutdown_calls, 1)
        self.assertEqual(fake.close_calls, 1)
        self.assertFalse(owner.listening)

    def test_local_service_shutdown_stops_owned_capture_usage_and_watchdog(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profiles = root / "profiles"
            profiles.mkdir()
            config = AgentConfiguration(root=root, profiles_dir=profiles, host="127.0.0.1", port=0, auth_token=None)
            service = controller.LocalService(config)
            relay = mock.Mock()
            usage = mock.Mock()
            service.capture_relays["fixture"] = relay
            service.usage = usage
            with mock.patch.object(service, "stop_all"), mock.patch.object(service, "stop_watchdog", create=True):
                service.stop_all_managed()
            relay.stop.assert_called_once_with()
            usage.stop.assert_called_once_with()
            self.assertEqual(service.capture_relays, {})


if __name__ == "__main__":
    unittest.main()
