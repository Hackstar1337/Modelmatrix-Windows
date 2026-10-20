from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Windows"))
sys.path.insert(0, str(ROOT / "RemoteAgent"))
import controller  # noqa: E402
from modelmatrix_agent import AgentConfiguration  # noqa: E402


FIXTURE = r'''
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
  def log_message(self, *a): pass
  def do_GET(self):
    body = json.dumps({"object":"list","data":[{"id":"fixture-model"}]}).encode()
    self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
  def do_POST(self):
    body = json.dumps({"id":"x","model":"fixture-model","choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":3,"completion_tokens":4}}).encode()
    self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class CaptureAcceptanceTests(unittest.TestCase):
    def test_capture_forwards_public_port_and_records_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profiles = root / "profiles with spaces"
            profiles.mkdir()
            fixture = root / "fixture with spaces.py"
            fixture.write_text(FIXTURE, encoding="utf-8")
            public = free_port()
            relay_port = free_port()
            values = {
                "REQUEST_MODEL": "fixture-model", "SERVER_MODEL_ID": "fixture-model",
                "RUNTIME": "llama.cpp", "PORT": str(public), "HOST": "127.0.0.1",
                "CAPTURE_MODE": "1", "HEALTHCHECK_MODE": "http-200",
                "START_COMMAND_JSON": json.dumps([sys.executable, str(fixture), "{port}"]),
            }
            (profiles / "fixture.env").write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
            config = AgentConfiguration(root=root, profiles_dir=profiles, host="127.0.0.1", port=free_port(), auth_token=None)
            config.relay_port = relay_port
            service = controller.LocalService(config)
            service.bootstrap()
            try:
                service.start("fixture")
                deadline = time.time() + 8
                while time.time() < deadline:
                    status = service.status_payload(["fixture"])["statuses"][0]
                    if status.get("ready") and status.get("pid"): break
                    time.sleep(.1)
                self.assertTrue(status.get("ready"), status)
                self.assertEqual(status["port"], str(public))
                self.assertEqual(status["pid"], service._read_pid("fixture"))
                with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{public}/v1/chat/completions", data=b'{"model":"fixture-model"}', headers={"Content-Type":"application/json"}), timeout=4) as response:
                    payload = json.loads(response.read())
                self.assertEqual(payload["usage"], {"prompt_tokens": 3, "completion_tokens": 4})
                rows = service.usage.store._rows("all")
                deadline = time.monotonic() + 2
                while not rows and time.monotonic() < deadline:
                    time.sleep(.01)
                    rows = service.usage.store._rows("all")
                self.assertEqual(len(rows), 1, rows)
                self.assertEqual(rows[0]["prompt_tokens"], 3)
                state = service.capture_state()[0]
                self.assertEqual(state["port"], public)
                self.assertEqual(state["internal_port"], service.capture_ports["fixture"])
                self.assertTrue(state["listening"])
                # Third-party clients must work with their scoped key alone,
                # even when the dashboard requires a DIFFERENT global token.
                http = controller.LocalHTTPServer(("127.0.0.1", 0), service, "global-token-not-the-scoped-key", root)
                threading.Thread(target=http.serve_forever, daemon=True).start()
                try:
                    bridge = service.bridge_local.impl
                    _, plan = bridge.route("POST", "/api/connect/plan", body={"profile": "fixture", "target": "manual"})
                    _, applied = bridge.route("POST", "/api/connect/apply", body={"plan_id": plan["plan"]["id"], "approved": True})
                    conn = applied["connection"]
                    key = bridge.secret_store.get("connection-" + conn["id"])
                    endpoint = conn["base_url"]
                    with urllib.request.urlopen(urllib.request.Request(endpoint + "/models", headers={"Authorization": "Bearer " + key}), timeout=4) as response:
                        self.assertEqual(json.loads(response.read())["data"][0]["id"], "fixture-model")
                    request = urllib.request.Request(endpoint + "/chat/completions", data=b'{"model":"fixture-model","messages":[{"role":"user","content":"hi"}]}', headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=4) as response:
                        self.assertEqual(json.loads(response.read())["choices"][0]["message"]["content"], "ok")
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(urllib.request.Request(endpoint + "/models", headers={"Authorization": "Bearer wrong"}), timeout=4)
                    self.assertEqual(raised.exception.code, 401)
                finally:
                    http.shutdown(); http.server_close()
            finally:
                service.stop_all_managed()
            with socket.socket() as probe:
                probe.settimeout(.2)
                self.assertRaises(OSError, probe.connect, ("127.0.0.1", public))


if __name__ == "__main__":
    unittest.main()
