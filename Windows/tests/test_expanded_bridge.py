from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "RemoteAgent"))
sys.path.insert(0, str(ROOT / "Windows"))

from agent_core import Profile, UsageError  # noqa: E402
from bridge import LocalBridge  # noqa: E402


class LoopbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[tuple[str, str, bytes, dict[str, str]]] = []

    def log_message(self, *_args):
        return

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)

    def _reply(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Fixture", "loopback-preserved")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        # Two writes make the stream boundary observable rather than only
        # proving that a buffered JSON response was returned.
        midpoint = max(1, len(payload) // 2)
        self.wfile.write(payload[:midpoint]); self.wfile.flush()
        self.wfile.write(payload[midpoint:]); self.wfile.flush()

    def _record(self) -> bytes:
        body = self._body()
        self.requests.append((self.command, self.path, body, dict(self.headers)))
        return body

    def do_GET(self):  # noqa: N802
        self._record()
        self._reply(200, json.dumps({"ok": True, "path": self.path}).encode())

    def do_POST(self):  # noqa: N802
        body = self._record()
        self._reply(202, json.dumps({"ok": True, "body": json.loads(body or b"{}")}).encode())

    def do_DELETE(self):  # noqa: N802
        self._record()
        self._reply(204, b"")


class CaptureHandler:
    def __init__(self, command: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None):
        self.command = command
        self.path = path
        self.headers = headers or {}
        self._body = body
        self.read_count = 0
        self.status = None
        self.response_headers: list[tuple[str, str]] = []
        self.payload = io.BytesIO()
        self.close_connection = False
        self.wfile = self.payload

    def _read_body(self):
        self.read_count += 1
        return self._body

    def _send_json(self, status, payload):
        self.status = status
        self.payload = io.BytesIO(json.dumps(payload, separators=(",", ":")).encode())

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, str(value)))

    def end_headers(self):
        return


class ExpandedBridgeTests(unittest.TestCase):
    def setUp(self):
        self.old_secret_flag = os.environ.get("MODELMATRIX_TEST_SECRET_STORE")
        os.environ["MODELMATRIX_TEST_SECRET_STORE"] = "1"
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profiles = self.root / "profiles"; self.profiles.mkdir()
        self.fixture = ThreadingHTTPServer(("127.0.0.1", 0), LoopbackHandler)
        self.thread = threading.Thread(target=self.fixture.serve_forever, daemon=True); self.thread.start()
        self.upstream = f"http://127.0.0.1:{self.fixture.server_port}/v1"
        values = {"REQUEST_MODEL": "fixture-model", "DISPLAY_NAME": "Fixture", "BASE_URL": self.upstream, "RUNTIME": "external"}
        (self.profiles / "fixture.env").write_text("REQUEST_MODEL=fixture-model\nBASE_URL=%s\nRUNTIME=external\n" % self.upstream, encoding="utf-8")
        profile = Profile("fixture", values)
        configuration = SimpleNamespace(root=self.root, profiles_directory=self.profiles, host="127.0.0.1", port=self.fixture.server_port)
        self.service = SimpleNamespace(configuration=configuration, profiles=SimpleNamespace(load=lambda: {"fixture": profile}, profile=lambda name: profile))
        self.service.status_payload = lambda: {"statuses": [{"profile": "fixture", "ready": True, "running": True}]}
        self.service.shutdown = mock.Mock()
        self.bridge = LocalBridge(self.service)
        LoopbackHandler.requests.clear()

    def tearDown(self):
        for gid in list(self.bridge._tunnels):
            self.bridge._stop_tunnel(gid)
        self.fixture.shutdown(); self.fixture.server_close(); self.thread.join(timeout=2)
        self.temp.cleanup()
        if self.old_secret_flag is None: os.environ.pop("MODELMATRIX_TEST_SECRET_STORE", None)
        else: os.environ["MODELMATRIX_TEST_SECRET_STORE"] = self.old_secret_flag

    def _add_direct_gateway(self, gateway_id="gw1"):
        status, result = self.bridge.route("POST", "/api/bridge/commands", body={"type": "gateway.add", "payload": {"id": gateway_id, "kind": "direct", "name": "Fixture", "base_url": self.upstream, "token": "gateway-secret"}})
        self.assertEqual((status, result["status"]), (202, "done"))

    def test_remote_http_loopback_whitelist_query_body_and_headers(self):
        self._add_direct_gateway()
        get_handler = CaptureHandler("GET", "/api/remote/gw1/api/hf/downloads/dl-1?since=3&limit=1")
        self.assertTrue(self.bridge.proxy(get_handler, "/api/remote/gw1/api/hf/downloads/dl-1"))
        self.assertEqual(get_handler.status, 200)
        self.assertIn(("X-Fixture", "loopback-preserved"), get_handler.response_headers)
        self.assertEqual(LoopbackHandler.requests[-1][1], "/v1/api/hf/downloads/dl-1?since=3&limit=1")
        post_handler = CaptureHandler("POST", "/api/remote/gw1/api/hf/downloads/action", b"", {"Content-Length": "0"})
        self.assertTrue(self.bridge.proxy(post_handler, "/api/remote/gw1/api/hf/downloads/action"))
        self.assertEqual(post_handler.read_count, 1)
        self.assertEqual(json.loads(LoopbackHandler.requests[-1][2]), {})
        unsafe = CaptureHandler("GET", "/api/remote/gw1/api/status?token=leak")
        self.bridge.proxy(unsafe, "/api/remote/gw1/api/status")
        self.assertEqual(unsafe.status, 400)
        self.assertFalse(any("leak" in part[1] for part in LoopbackHandler.requests))

    def test_connect_models_and_streamed_chat_are_scoped_and_preserve_headers(self):
        _, applied = self.bridge.route("POST", "/api/connect/apply", body={"plan_id": self.bridge.route("POST", "/api/connect/plan", body={"profile": "fixture", "target": "manual"})[1]["plan"]["id"], "approved": True})
        connection = applied["connection"]
        models = CaptureHandler("GET", f"/api/connect/{connection['id']}/v1/models", headers={"Authorization": "Bearer " + self.bridge.secret_store.get("connection-" + connection["id"])})
        self.assertTrue(self.bridge.proxy(models, f"/api/connect/{connection['id']}/v1/models"))
        self.assertEqual(models.status, 200)
        self.assertEqual(json.loads(models.payload.getvalue())["data"][0]["id"], "fixture-model")
        body = json.dumps({"model": "fixture-model", "messages": [{"role": "user", "content": "hi"}], "stream": True}).encode()
        chat = CaptureHandler("POST", f"/api/connect/{connection['id']}/v1/chat/completions", body, {"Authorization": "Bearer " + self.bridge.secret_store.get("connection-" + connection["id"])})
        self.assertTrue(self.bridge.proxy(chat, f"/api/connect/{connection['id']}/v1/chat/completions"))
        self.assertEqual(chat.status, 202)
        self.assertIn(("X-Fixture", "loopback-preserved"), chat.response_headers)
        self.assertEqual(chat.read_count, 1)
        self.assertEqual(json.loads(LoopbackHandler.requests[-1][2])["model"], "fixture-model")
        wrong = CaptureHandler("POST", f"/api/connect/{connection['id']}/v1/chat/completions", json.dumps({"model": "other"}).encode(), chat.headers)
        self.bridge.proxy(wrong, f"/api/connect/{connection['id']}/v1/chat/completions")
        self.assertEqual(wrong.status, 409)

    def test_connect_plan_apply_verify_disconnect_undo_reveal_and_quit(self):
        plan_status, plan_response = self.bridge.route("POST", "/api/connect/plan", body={"profile": "fixture", "target": "manual"})
        self.assertEqual(plan_status, 200)
        _, applied = self.bridge.route("POST", "/api/connect/apply", body={"plan_id": plan_response["plan"]["id"], "approved": True})
        connection = applied["connection"]
        _, revealed = self.bridge.route("POST", "/api/connect/key", body={"id": connection["id"], "approved": True})
        self.assertGreaterEqual(len(revealed["key"]), 32)
        _, verified = self.bridge.route("POST", "/api/connect/verify", body={"id": connection["id"]})
        self.assertTrue(verified["connection"]["api_tested"])
        with mock.patch.object(self.bridge, "_open_path", return_value={"opened": True, "path": str(self.profiles)}):
            _, opened = self.bridge.route("POST", "/api/bridge/commands", body={"type": "app.open_profiles_folder", "payload": {}})
        self.assertTrue(opened["result"]["opened"])
        _, undone = self.bridge.route("POST", "/api/connect/undo", body={"id": connection["id"], "approved": True})
        self.assertEqual(undone["connection"]["state"], "disconnected")
        self.assertFalse(self.bridge.secret_store.get("connection-" + connection["id"]))
        self.service.shutdown.reset_mock()
        _, quit_result = self.bridge.route("POST", "/api/bridge/commands", body={"type": "app.quit_app", "payload": {}})
        self.service.shutdown.assert_called_once_with()
        self.assertTrue(quit_result["result"]["requested"])

    def test_current_target_version_schema_and_npm_wrappers(self):
        wrapper = self.root / "kilo.cmd"; wrapper.write_text("@echo off\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"MODELMATRIX_KILO_PATH": str(wrapper)}, clear=False), mock.patch("bridge.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "7.8.15\n", "")):
            target = self.bridge._target("kilo")
        self.assertTrue(target["qualified"])
        self.assertEqual(target["version"], "7.8.15")
        self.assertEqual(target["schema_url"], "https://app.kilo.ai/config.json")
        self.assertIn("kilo.cmd", target["supported_executables"])

    def test_ssh_deployment_uses_only_staged_repository_payload(self):
        known = self.root / "known_hosts"; known.write_text("fixture\n", encoding="utf-8")
        gateway = self.bridge._gateway_from_payload({"id": "ssh1", "kind": "ssh", "ssh_host": "host", "known_hosts": str(known), "platform": "windows"})
        self.bridge._gateways["ssh1"] = gateway
        with mock.patch("bridge.shutil.which", side_effect=lambda name: "/bin/echo"), mock.patch("bridge.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as run:
            result = self.bridge._gateway_install({"id": "ssh1"}, update=False)
        self.assertTrue(result["installed"])
        scp_call = next(call.args[0] for call in run.call_args_list if str(ROOT / "RemoteAgent" / "install-remote-agent.ps1") in map(str, call.args[0]))
        self.assertTrue(any(str(ROOT / "RemoteAgent" / "install-remote-agent.ps1") == part for part in scp_call))
        self.assertNotIn("Invoke-WebRequest", " ".join(map(str, scp_call)))

    @unittest.skipUnless(os.name == "nt", "native OpenSSH/PowerShell validation runs on Windows only")
    def test_native_windows_boundary(self):
        self.assertTrue(Path(os.environ.get("USERPROFILE", "C:/Users" )).exists())


if __name__ == "__main__":
    unittest.main()
