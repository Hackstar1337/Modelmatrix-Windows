from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "RemoteAgent"))
sys.path.insert(0, str(ROOT / "Windows"))

from agent_core import InvalidJSONError, OperationFailedError, Profile, ProfileConflictError, UsageError  # noqa: E402
import bridge  # noqa: E402
from bridge import LocalBridge  # noqa: E402
from client_connections import forward_connection  # noqa: E402


class CaptureHandler:
    def __init__(self, command: str, path: str, body: bytes = b"", headers=None):
        self.command = command
        self.path = path
        self.headers = headers or {}
        self.body = body
        self.status = None
        self.response_headers = []
        self.payload = io.BytesIO()
        self.wfile = self.payload
        self.close_connection = False
        self.read_count = 0

    def _read_body(self):
        self.read_count += 1
        return self.body

    def _send_json(self, status, payload):
        self.status = status
        self.payload = io.BytesIO(json.dumps(payload, separators=(",", ":")).encode())

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, str(value)))

    def end_headers(self):
        return None


class ModelHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        return None

    def _read(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        self.requests.append((self.command, self.path, body, dict(self.headers)))
        return body

    def do_POST(self):  # noqa: N802
        body = self._read()
        payload = json.dumps({"choices": [{"message": {"content": "ok"}}], "body": json.loads(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        self._read()
        payload = b'{"object":"list","data":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class RelayHandler(BaseHTTPRequestHandler):
    bridge_instance = None

    def log_message(self, *_args):
        return None

    def _read_body(self):
        size = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(size)

    def _send_json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not self.bridge_instance.proxy(self, path):
            self._send_json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not self.bridge_instance.proxy(self, path):
            self._send_json(404, {"error": "not_found"})


class PCBridgeCoverage(unittest.TestCase):
    def setUp(self):
        self.old_secret_flag = os.environ.get("MODELMATRIX_TEST_SECRET_STORE")
        os.environ["MODELMATRIX_TEST_SECRET_STORE"] = "1"
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir()
        self.model_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        self.model_thread = threading.Thread(target=self.model_server.serve_forever, daemon=True)
        self.model_thread.start()
        base = f"http://127.0.0.1:{self.model_server.server_port}/v1"
        (self.profiles / "fixture.env").write_text(
            f"REQUEST_MODEL=fixture-model\nBASE_URL={base}\nRUNTIME=external\n",
            encoding="utf-8",
        )
        profile = Profile("fixture", {"REQUEST_MODEL": "fixture-model", "BASE_URL": base, "RUNTIME": "external"})
        self.service = SimpleNamespace(
            configuration=SimpleNamespace(root=self.root, profiles_directory=self.profiles, host="127.0.0.1", port=0),
            profiles=SimpleNamespace(load=lambda: {"fixture": profile}, profile=lambda name: profile),
        )
        self.service.status_payload = lambda: {"statuses": [{"profile": "fixture", "ready": True, "running": True}]}
        self.service.shutdown = mock.Mock()
        self.bridge = LocalBridge(self.service)
        RelayHandler.bridge_instance = self.bridge
        self.relay_server = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        self.service.configuration.port = self.relay_server.server_port
        self.relay_thread = threading.Thread(target=self.relay_server.serve_forever, daemon=True)
        self.relay_thread.start()
        ModelHandler.requests.clear()

    def tearDown(self):
        for gid in list(self.bridge._tunnels):
            self.bridge._stop_tunnel(gid)
        self.relay_server.shutdown(); self.relay_server.server_close(); self.relay_thread.join(timeout=2)
        self.model_server.shutdown(); self.model_server.server_close(); self.model_thread.join(timeout=2)
        self.temp.cleanup()
        if self.old_secret_flag is None:
            os.environ.pop("MODELMATRIX_TEST_SECRET_STORE", None)
        else:
            os.environ["MODELMATRIX_TEST_SECRET_STORE"] = self.old_secret_flag

    def route(self, method, path, **body):
        return self.bridge.route(method, path, body=body)

    def test_manual_plan_apply_verify_remove_and_rollback_are_scoped(self):
        with self.assertRaises(ProfileConflictError):
            self.route("POST", "/api/connect/apply", plan_id="missing", approved=True)
        status, plan_result = self.route("POST", "/api/connect/plan", profile="fixture", target="manual")
        self.assertEqual(status, 200)
        plan = plan_result["plan"]
        self.assertIn("no config file is changed", plan["changes"][0])
        self.assertNotIn("Bearer ", json.dumps(plan))
        with self.assertRaises(UsageError):
            self.route("POST", "/api/connect/apply", plan_id=plan["id"], approved=False)
        _, applied = self.route("POST", "/api/connect/apply", plan_id=plan["id"], approved=True)
        connection = applied["connection"]
        self.assertTrue(connection["configured"])
        self.assertTrue(Path(connection["key_file"]).is_file())
        blocked = None
        try:
            _, verified = self.route("POST", "/api/connect/verify", id=connection["id"])
            self.assertTrue(verified["connection"]["api_tested"])
            self.assertEqual(ModelHandler.requests[-1][1], "/v1/chat/completions")
        except OperationFailedError as error:
            chain = repr(error)
            cause = error.__cause__
            while cause is not None:
                chain += repr(cause)
                cause = getattr(cause, "__cause__", None)
            if "10013" not in chain:
                raise
            blocked = error
        _, undone = self.route("POST", "/api/connect/undo", id=connection["id"], approved=True)
        self.assertEqual(undone["connection"]["state"], "disconnected")
        self.assertFalse(Path(connection["key_file"]).exists())
        self.assertIsNone(self.bridge.secret_store.get("connection-" + connection["id"]))
        if blocked is not None:
            self.skipTest("BLOCKED: Windows policy denied loopback HTTP (WinError 10013); model API verification is not a PASS")

    def test_qualified_client_plan_apply_and_disconnect_only_mutate_owned_provider(self):
        config = self.root / "kilo.jsonc"
        config.write_text('{"provider":{"keep":{"id":"untouched"}}}\n', encoding="utf-8")
        executable = self.root / "kilo.exe"
        executable.write_text("fixture", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"MODELMATRIX_KILO_PATH": str(executable), "MODELMATRIX_KILO_CONFIG": str(config)},
            clear=False,
        ), mock.patch.object(
            bridge.subprocess,
            "run",
            return_value=SimpleNamespace(stdout="kilo 7.8.15\n", stderr="", returncode=0),
        ):
            _, planned = self.route("POST", "/api/connect/plan", profile="fixture", target="kilo")
            plan = planned["plan"]
            self.assertTrue(plan["config_path"].endswith("kilo.jsonc"))
            _, applied = self.route("POST", "/api/connect/apply", plan_id=plan["id"], approved=True)
        connection = applied["connection"]
        document = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(document["provider"]["keep"], {"id": "untouched"})
        self.assertIn(connection["provider_id"], document["provider"])
        self.assertNotIn(self.bridge.secret_store.get("connection-" + connection["id"]), config.read_text(encoding="utf-8"))
        _, disconnected = self.route("POST", "/api/connect/disconnect", id=connection["id"], approved=True)
        self.assertEqual(disconnected["connection"]["state"], "disconnected")
        self.assertEqual(json.loads(config.read_text(encoding="utf-8")), {"provider": {"keep": {"id": "untouched"}}})

    def test_direct_gateway_routes_require_safe_path_query_and_token(self):
        with self.assertRaises(UsageError):
            self.route("POST", "/api/bridge/commands", type="gateway.add", payload={"id": "external", "kind": "direct", "base_url": "https://remote.example"})
        with self.assertRaises(UsageError):
            self.route("POST", "/api/bridge/commands", type="gateway.add", payload={"id": "creds", "kind": "direct", "base_url": "http://user:pass@127.0.0.1:1", "token": "x"})
        self.route("POST", "/api/bridge/commands", type="gateway.add", payload={"id": "loop", "kind": "direct", "base_url": f"http://127.0.0.1:{self.model_server.server_port}", "token": "gateway-secret"})
        unsafe = CaptureHandler("GET", "/api/remote/loop/api/status?token=leak")
        self.assertTrue(self.bridge.proxy(unsafe, "/api/remote/loop/api/status"))
        self.assertEqual(unsafe.status, 400)
        self.assertFalse(any("leak" in h.get("Authorization", "") for _, _, _, h in ModelHandler.requests))
        bad_path = CaptureHandler("GET", "/api/remote/loop/api/status/../secret")
        self.assertTrue(self.bridge.proxy(bad_path, "/api/remote/loop/api/status/../secret"))
        self.assertEqual(bad_path.status, 404)
        self.route("POST", "/api/bridge/commands", type="gateway.toggle", payload={"id": "loop", "enabled": False})
        disabled = CaptureHandler("GET", "/api/remote/loop/api/status")
        self.assertTrue(self.bridge.proxy(disabled, "/api/remote/loop/api/status"))
        self.assertEqual(disabled.status, 503)

    def test_supported_client_executable_inspection_is_read_only_and_exact(self):
        fake = self.root / "kilo.cmd"
        fake.write_text("@echo off\n", encoding="utf-8")
        before = list(self.profiles.iterdir())
        with mock.patch.dict(os.environ, {"MODELMATRIX_KILO_PATH": str(fake)}, clear=False), mock.patch.object(bridge.subprocess, "run", return_value=SimpleNamespace(stdout="kilo 0.1.0\n", stderr="")):
            target = self.bridge._target("kilo")
        self.assertEqual(target["version"], "0.1.0")
        self.assertFalse(target["qualified"])
        self.assertIn("kilo.cmd", target["supported_executables"])
        self.assertEqual(before, list(self.profiles.iterdir()))

    def test_remote_agent_connection_rejects_wrong_identity_model_endpoint_and_bad_json(self):
        service = self.service
        metadata = bridge.connection_profiles(service)["profiles"][0]
        handler = CaptureHandler("POST", f"/api/connect/inference?profile=fixture&endpoint=/v1/chat/completions&identity={metadata['identity_token']}", b"not-json")
        handler.service = service
        with self.assertRaises(InvalidJSONError):
            forward_connection(handler, handler.body)
        handler = CaptureHandler("POST", f"/api/connect/inference?profile=fixture&endpoint=/v1/chat/completions&identity=wrong", b'{"model":"fixture-model"}')
        handler.service = service
        with self.assertRaises(ProfileConflictError):
            forward_connection(handler, handler.body)
        handler = CaptureHandler("POST", f"/api/connect/inference?profile=fixture&endpoint=/v1/chat/completions&identity={metadata['identity_token']}", b'{"model":"other"}')
        handler.service = service
        with self.assertRaises(UsageError):
            forward_connection(handler, handler.body)
        handler = CaptureHandler("GET", f"/api/connect/inference?profile=fixture&endpoint=/v1/unknown&identity={metadata['identity_token']}")
        handler.service = service
        with self.assertRaises(UsageError):
            forward_connection(handler, b"")


if __name__ == "__main__":
    unittest.main()
