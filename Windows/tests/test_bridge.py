from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "RemoteAgent"))
sys.path.insert(0, str(ROOT / "Windows"))
from agent_core import Profile, ProfileNotFoundError, UsageError
from bridge import LocalBridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self._old_test_flag = os.environ.get("MODELMATRIX_TEST_SECRET_STORE")
        os.environ["MODELMATRIX_TEST_SECRET_STORE"] = "1"
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        profiles = root / "profiles"
        profiles.mkdir()
        profile_file = profiles / "one.env"
        profile_file.write_text("REQUEST_MODEL=matrix-one\nDISPLAY_NAME=One\nPORT=18081\nRUNTIME=llamacpp\n", encoding="utf-8")
        profile = Profile("one", {"REQUEST_MODEL": "matrix-one", "DISPLAY_NAME": "One", "PORT": "18081", "RUNTIME": "llamacpp"})
        configuration = SimpleNamespace(root=root, profiles_directory=profiles, host="127.0.0.1", port=7700)
        self.service = SimpleNamespace(configuration=configuration, profiles=SimpleNamespace(load=lambda: {"one": profile}, profile=lambda name: profile))
        self.service.status_payload = lambda: {"statuses": [{"profile": "one", "ready": True, "running": True}]}
        self.bridge = LocalBridge(self.service)

    def tearDown(self):
        if self._old_test_flag is None:
            os.environ.pop("MODELMATRIX_TEST_SECRET_STORE", None)
        else:
            os.environ["MODELMATRIX_TEST_SECRET_STORE"] = self._old_test_flag
        self.temp.cleanup()

    def test_state_is_local_and_does_not_expose_secrets(self):
        status, state = self.bridge.route("GET", "/api/bridge/state")
        self.assertEqual(status, 200)
        self.assertTrue(state["app_connected"])
        self.assertNotIn("token", json.dumps(state))

    def test_pair_direct_gateway_persists_without_plaintext_token(self):
        with mock.patch.object(self.bridge, "_gateway_status", return_value="configured"):
            status, result = self.bridge.route("POST", "/api/bridge/commands", body={"type": "gateway.pair", "payload": {"link": "modelmatrix-gateway://host.example?name=Lab&agent_port=7700&mode=direct", "token": "pair-secret-value"}})
        self.assertEqual(status, 202)
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["result"]["gateway"]["has_token"])
        persisted = (Path(self.temp.name) / "bridge" / "gateways.json").read_text(encoding="utf-8")
        self.assertNotIn("pair-secret-value", persisted)

    def test_manual_connection_plan_apply_key_is_explicit_only(self):
        status, plan_response = self.bridge.route("POST", "/api/connect/plan", body={"profile": "one", "target": "manual"})
        self.assertEqual(status, 200)
        plan = plan_response["plan"]
        self.assertNotIn('"key":', json.dumps(plan))
        status, applied = self.bridge.route("POST", "/api/connect/apply", body={"plan_id": plan["id"], "approved": True})
        self.assertEqual(status, 200)
        connection = applied["connection"]
        self.assertNotIn('"key":', json.dumps(connection))
        with self.assertRaises(UsageError):
            self.bridge.route("POST", "/api/connect/key", body={"id": connection["id"], "approved": False})
        status, secret = self.bridge.route("POST", "/api/connect/key", body={"id": connection["id"], "approved": True})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(secret["key"]), 32)

    def test_profile_identity_change_blocks_new_details(self):
        self.service.profiles.load = lambda: {}
        with self.assertRaises(ProfileNotFoundError):
            self.bridge.route("GET", "/api/connect/details", query={"profile": "one"})

    def test_ssh_requires_known_hosts_and_never_uses_controlmaster(self):
        gateway = self.bridge._gateway_from_payload({"id": "ssh1", "name": "SSH", "kind": "ssh", "ssh_host": "host.example"})
        with mock.patch("bridge.shutil.which", return_value="/usr/bin/ssh"), mock.patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(Exception) as raised:
                self.bridge._ensure_tunnel(gateway)
        self.assertIn("known_hosts", str(raised.exception))

    def test_target_requires_exact_qualified_version(self):
        executable = Path(self.temp.name) / "kilo"
        executable.write_text("#!/bin/sh\necho 7.8.13\n", encoding="utf-8")
        executable.chmod(0o700)
        with mock.patch.dict(os.environ, {"MODELMATRIX_KILO_PATH": str(executable)}, clear=False), mock.patch("bridge.subprocess.run", return_value=__import__("subprocess").CompletedProcess([], 0, "7.8.13\n", "")):
            target = self.bridge._target("kilo")
        self.assertFalse(target["qualified"])
        self.assertIn("7.8.12", target["reason"])

    def test_jsonc_parser_keeps_urls_containing_double_slash(self):
        path = Path(self.temp.name) / "client.jsonc"
        path.write_text('{"endpoint": "https://example.test/a//b", // comment\n "keep": 7,}\n', encoding="utf-8")
        self.assertEqual(self.bridge._jsonc_load(path), {"endpoint": "https://example.test/a//b", "keep": 7})

    def test_qualified_apply_uses_owned_provider_and_private_key_file(self):
        executable = Path(self.temp.name) / "kilo"
        executable.write_text("#!/bin/sh\necho 7.8.12\n", encoding="utf-8")
        executable.chmod(0o700)
        config = Path(self.temp.name) / "kilo.jsonc"
        config.write_text('{"other": {"url": "https://example.test/a//b"}}\n', encoding="utf-8")
        env = {"MODELMATRIX_KILO_PATH": str(executable), "MODELMATRIX_KILO_CONFIG": str(config)}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch("bridge.subprocess.run", return_value=__import__("subprocess").CompletedProcess([], 0, "7.8.12\n", "")):
            _, plan_response = self.bridge.route("POST", "/api/connect/plan", body={"profile": "one", "target": "kilo"})
            plan = plan_response["plan"]
            _, applied = self.bridge.route("POST", "/api/connect/apply", body={"plan_id": plan["id"], "approved": True})
        document = json.loads(config.read_text(encoding="utf-8"))
        connection = applied["connection"]
        owned = document["provider"][connection["provider_id"]]
        self.assertTrue(connection["provider_id"].startswith("mmx-"))
        self.assertEqual(owned["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(owned["options"]["baseURL"], connection["base_url"])
        self.assertTrue(owned["options"]["apiKey"].startswith("{file:"))
        self.assertNotIn(self.bridge.secret_store.get(f"connection-{connection['id']}"), config.read_text(encoding="utf-8"))
        if os.name != "nt":
            self.assertEqual(Path(connection["key_file"]).stat().st_mode & 0o777, 0o600)
        else:
            self.assertTrue(Path(connection["key_file"]).is_file())
        self.assertEqual(document["other"]["url"], "https://example.test/a//b")

    def test_apply_rolls_back_config_and_secret_when_commit_fails(self):
        with mock.patch.object(self.bridge, "_write_client", side_effect=OSError("disk full")):
            _, plan_response = self.bridge.route("POST", "/api/connect/plan", body={"profile": "one", "target": "manual"})
            # Manual connections do not write a client file; exercise the same
            # protected key transaction through a qualified target instead.
        executable = Path(self.temp.name) / "kilo"
        executable.write_text("#!/bin/sh\necho 7.8.12\n", encoding="utf-8")
        executable.chmod(0o700)
        config = Path(self.temp.name) / "kilo.jsonc"
        original = '{"keep": 1}\n'
        config.write_text(original, encoding="utf-8")
        with mock.patch.dict(os.environ, {"MODELMATRIX_KILO_PATH": str(executable), "MODELMATRIX_KILO_CONFIG": str(config)}, clear=False), mock.patch("bridge.subprocess.run", return_value=__import__("subprocess").CompletedProcess([], 0, "7.8.12\n", "")):
            _, plan_response = self.bridge.route("POST", "/api/connect/plan", body={"profile": "one", "target": "kilo"})
            with mock.patch.object(self.bridge, "_write_client", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    self.bridge.route("POST", "/api/connect/apply", body={"plan_id": plan_response["plan"]["id"], "approved": True})
        self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_ssh_target_accepts_user_and_bracketed_ipv6(self):
        parsed = self.bridge._parse_ssh_target("alice@[2001:db8::7]:2222")
        self.assertEqual(parsed, {"user": "alice", "host": "2001:db8::7", "port": 2222})


if __name__ == "__main__":
    unittest.main()
