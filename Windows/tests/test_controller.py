from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Windows"))
sys.path.insert(0, str(ROOT / "RemoteAgent"))
import controller  # noqa: E402
from modelmatrix_agent import AgentConfiguration  # noqa: E402


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir()
        (self.profiles / "nested.json").write_text(
            '{\r\n  "REQUEST_MODEL": "demo",\r\n  "PORT": 19091,\r\n  "LAUNCH_MODE": "adapter",\r\n  "START_COMMAND": "fixture",\r\n  "TAGS": ["a", "b"]\r\n}\r\n', encoding="utf-8"
        )
        self.config = AgentConfiguration(
            root=self.root, profiles_dir=self.profiles, host="127.0.0.1", port=17701, auth_token="t" * 32
        )
        self.service = controller.LocalService(self.config)
        # The service's inherited start(name) must remain available; bootstrap
        # is deliberately separate from lifecycle start.
        self.server = None

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.service.stop_all_managed()
        self.temp.cleanup()

    def _start_http(self) -> str:
        self.service.bootstrap()
        self.server = controller.LocalHTTPServer(("127.0.0.1", 0), self.service, self.config.auth_token, self.root)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}"

    @staticmethod
    def _request(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, dict(response.headers), json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), json.loads(error.read())

    def test_localservice_start_is_inherited_and_body_is_forwarded_once(self) -> None:
        with mock.patch.object(self.service, "_spawn_profile") as spawn:
            self.service.start("nested")
            spawn.assert_called_once()

        base = self._start_http()
        cookie_status, cookie_headers, _ = self._request(base + "/api/dashboard/session", method="POST", body={"token": "t" * 32}, headers={"Origin": base})
        self.assertEqual(cookie_status, 200)
        cookie = cookie_headers["Set-Cookie"].split(";", 1)[0]
        # /api/config/profiles-dir is an inherited POST route. If the local
        # handler consumes the body and delegates without caching it, this
        # request either hangs or returns a malformed-body error.
        with mock.patch.object(self.service, "set_profiles_directory", return_value={"profiles_dir": str(self.profiles.resolve())}):
            status, _, payload = self._request(
                base + "/api/config/profiles-dir", method="POST", body={"profiles_dir": str(self.profiles)},
                headers={"Cookie": cookie, "Origin": base},
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("profiles_dir"), str(self.profiles.resolve()))

    def test_session_requires_login_revokes_cookie_and_bearer_mutation_works(self) -> None:
        base = self._start_http()
        status, headers, payload = self._request(base + "/api/dashboard/session")
        self.assertEqual(status, 200)
        self.assertFalse(payload["authenticated"])
        self.assertNotIn("Set-Cookie", headers)
        status, _, _ = self._request(base + "/api/dashboard/info")
        self.assertEqual(status, 401)
        status, _, _ = self._request(base + "/api/dashboard/session", method="POST", body={"token": "t" * 32}, headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, headers, _ = self._request(base + "/api/dashboard/session", method="POST", body={"token": "t" * 32}, headers={"Origin": base})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, payload = self._request(base + "/api/dashboard/info", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(set(("name", "version", "controller_url", "token_required", "profiles_dir", "controller_root", "relay", "platform", "capabilities")), set(payload))
        status, _, _ = self._request(base + "/api/dashboard/session", method="DELETE", headers={"Cookie": cookie, "Origin": base})
        self.assertEqual(status, 200)
        status, _, _ = self._request(base + "/api/dashboard/info", headers={"Cookie": cookie})
        self.assertEqual(status, 401)
        self.service.start = mock.Mock()  # type: ignore[method-assign]
        status, _, _ = self._request(base + "/api/start", method="POST", body={"profile": "nested"}, headers={"Authorization": "Bearer " + "t" * 32})
        self.assertEqual(status, 200)
        self.service.start.assert_called_once_with("nested")

    def test_invalid_host_is_rejected_for_static_and_servers_do_not_share_state(self) -> None:
        base = self._start_http()
        status, _, _ = self._request(base + "/", headers={"Host": "attacker.example"})
        self.assertEqual(status, 421)
        other_service = controller.LocalService(AgentConfiguration(root=self.root / "other", profiles_dir=self.root / "other-profiles", auth_token="u" * 32))
        other_service.configuration.profiles_directory.mkdir(parents=True)
        other = controller.LocalHTTPServer(("127.0.0.1", 0), other_service, "u" * 32, self.root)
        try:
            self.assertIs(self.server.service, self.service)
            self.assertIs(other.service, other_service)
        finally:
            other.server_close()
            other_service.stop_all_managed()

    def test_json_profile_document_uses_original_normalization_and_crlf_is_accepted(self) -> None:
        status, document = self.service.profiles_local.route("GET", "/api/profiles/document", {"profile": "nested"}, {})
        self.assertEqual(status, 200)
        self.assertEqual(document["format"], "json")
        self.assertEqual(Path(document["file"]).resolve(), (self.profiles / "nested.json").resolve())
        self.assertEqual(document["values"]["PORT"], "19091")
        self.assertEqual(document["values"]["TAGS"], '["a","b"]')
        status, response = self.service.profiles_local.route("POST", "/api/profiles/save", {"profile": "nested"}, {"profile": "nested", "text": '{\r\n"REQUEST_MODEL":"changed",\r\n"PORT":19092\r\n}'})
        self.assertEqual(status, 200)
        self.assertEqual(response["values"]["PORT"], "19092")
        self.assertIn("19092", (self.profiles / "nested.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
