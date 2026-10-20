"""Expanded Windows-support contracts; native process tests skip on non-Windows.

Mocks exercise Windows branches on Linux.  The native test is intentionally
kept separate because Job Objects and netstat behavior cannot be proved by a
POSIX host.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import agent_core  # noqa: E402
import client_connections as connection  # noqa: E402
import discovery  # noqa: E402
import modelmatrix_agent as agent  # noqa: E402
import platform_support as ps  # noqa: E402


class _Response:
    status = 404
    headers = {"Content-Type": "application/json"}

    def __init__(self, body: bytes = b'{"error":"upstream"}'):
        self.body = io.BytesIO(body)

    def read1(self, size: int) -> bytes:
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Handler:
    def __init__(self, service, path: str):
        self.service = service
        self.path = path
        self.wfile = io.BytesIO()
        self.headers_sent = {}
        self.status = None
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers_sent[key] = value

    def end_headers(self):
        pass

    def _send_json(self, status, body):
        self.status = status
        self.wfile.write(json.dumps(body).encode())


class WindowsStructuredLaunchTests(unittest.TestCase):
    def test_batch_argv_is_explicitly_cmd_and_list2cmdline_safe(self):
        with mock.patch.object(ps, "IS_WINDOWS", True):
            argv, shell = ps.command_argv(
                {"START_COMMAND_JSON": json.dumps([r"C:\Program Files\Model\serve.cmd", "a & b", 'q"uote'])},
                "ignored",
            )
        self.assertEqual(shell, "cmd")
        self.assertEqual(argv[0], r"C:\Program Files\Model\serve.cmd")
        self.assertEqual(argv[1:], ["a & b", 'q"uote'])

    def test_cmd_wrapper_quotes_multiple_arguments(self):
        fake = mock.Mock(pid=1234)
        with mock.patch.object(ps, "IS_WINDOWS", True), \
             mock.patch.object(ps.subprocess, "Popen", return_value=fake) as popen, \
             mock.patch.object(ps.WindowsProcessAdapter, "_assign_job"), \
             mock.patch.object(ps.WindowsProcessAdapter, "_resume_process"), \
             mock.patch.object(ps, "process_identity", return_value=None):
            ps.WindowsProcessAdapter().launch(
                [r"C:\Program Files\Model\serve.cmd", "a & b"],
                cwd=Path("."), env={}, log_handle=None, shell="cmd",
            )
        command = popen.call_args.args[0][-1]
        self.assertIn("Program Files", command)
        self.assertIn('"a & b"', command)

    def test_venv_only_overrides_bare_python(self):
        profile_values = {
            "REQUEST_MODEL": "m", "PORT": "19101", "RUNTIME": "sglang",
            "VENV": r"C:\env with spaces",
            "START_COMMAND_JSON": json.dumps(["python", "-m", "server"]),
        }
        profile = agent.Profile("p", profile_values)
        fake = mock.Mock(pid=1234)
        with mock.patch.object(agent, "IS_WINDOWS", True), \
             mock.patch.object(agent._WINDOWS_PROCESS_ADAPTER, "launch", return_value=fake) as launch:
            agent._popen_profile_bash("ignored", None, agent._spawn_environment(profile, "p"), canonical="p", cwd=Path("."))
        self.assertEqual(launch.call_args.args[0][0], r"C:\env with spaces\Scripts\python.exe")

        explicit = dict(profile_values)
        explicit["START_COMMAND_JSON"] = json.dumps([r"D:\Python\python.exe", "-m", "server"])
        profile = agent.Profile("explicit", explicit)
        with mock.patch.object(agent, "IS_WINDOWS", True), \
             mock.patch.object(agent._WINDOWS_PROCESS_ADAPTER, "launch", return_value=fake) as launch:
            agent._popen_profile_bash("ignored", None, agent._spawn_environment(profile, "explicit"), canonical="explicit", cwd=Path("."))
        self.assertEqual(launch.call_args.args[0][0], r"D:\Python\python.exe")

    def test_apple_runtime_is_explicitly_unsupported(self):
        profile = agent.Profile("mlx", {"REQUEST_MODEL": "m", "PORT": "19102", "RUNTIME": "mlx"})
        with mock.patch.object(agent, "IS_WINDOWS", True):
            with self.assertRaises(agent.UnsupportedError) as error:
                agent.build_start_command(profile)
        self.assertIn("not supported natively on Windows", str(error.exception))


class WindowsProfileDiscoveryTests(unittest.TestCase):
    def test_reserved_profile_log_and_pid_components_are_safe(self):
        profile = agent_core.Profile("CON", {"REQUEST_MODEL": "m", "PORT": "19103"})
        with mock.patch.object(agent_core, "IS_WINDOWS", True), mock.patch.object(ps, "IS_WINDOWS", True):
            self.assertNotIn("CON.log", profile.log_path.upper())
        service = agent.AgentService.__new__(agent.AgentService)
        service.configuration = SimpleNamespace(run_directory=Path("."))
        with mock.patch.object(agent, "IS_WINDOWS", True), mock.patch.object(ps, "IS_WINDOWS", True):
            self.assertNotEqual(service._pid_file("CON").name, "CON.pid")

    def test_case_insensitive_duplicate_profiles_keep_one_deterministically(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "Model.env").write_text("REQUEST_MODEL=one\nPORT=19104\n", encoding="utf-8")
            (directory / "model.json").write_text(json.dumps({"REQUEST_MODEL": "two", "PORT": "19105"}), encoding="utf-8")
            with mock.patch.object(agent, "IS_WINDOWS", True):
                profiles = agent.ProfileRepository(directory).load()
                self.assertEqual(len(profiles), 1)
                self.assertEqual(next(iter(profiles.values())).request_model, "one")
                self.assertEqual(agent.ProfileRepository(directory).profile("MODEL").request_model, "one")

    def test_windows_claim_scripts_are_discovered_without_execute_bit(self):
        with tempfile.TemporaryDirectory() as tmp:
            claim = Path(tmp) / "19106"
            claim.mkdir()
            (claim / "flags.env").write_text("MODEL=/models/demo.gguf\n", encoding="utf-8")
            (claim / "launch.cmd").write_text("@echo off\r\n", encoding="utf-8")
            with mock.patch.object(discovery, "IS_WINDOWS", True):
                claims = discovery.scan_port_claim_directories(roots=[Path(tmp)], listeners=[])
                self.assertEqual(len(claims), 1)
                self.assertIn("launch.cmd", claims[0]["markers"])
                profile = discovery.profile_from_claim(claims[0])
                self.assertEqual(profile.get("SHELL"), "cmd")


class ExpandedConnectionAndBenchmarkTests(unittest.TestCase):
    def _service(self, root: Path):
        folder = root / "profiles"
        folder.mkdir()
        (folder / "one.env").write_text("REQUEST_MODEL=matrix-one\nPORT=19107\n", encoding="utf-8")
        profile = agent_core.Profile("one", {"REQUEST_MODEL": "matrix-one", "PORT": "19107", "UPSTREAM_MODEL": "wire-one"})
        service = SimpleNamespace(
            configuration=SimpleNamespace(profiles_directory=folder),
            profiles=SimpleNamespace(load=lambda: {"one": profile}, profile=lambda _name: profile),
            status_payload=lambda: {"statuses": [{"profile": "one", "ready": True, "running": True}]},
        )
        return service

    def test_get_models_forwards_empty_body_and_upstream_http_error_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            identity = connection.connection_profiles(service)["profiles"][0]["identity_token"]
            handler = _Handler(service, "/api/connect/inference?profile=one&identity=" + identity + "&endpoint=/v1/models")
            opener = mock.Mock()
            opener.open.return_value = _Response()
            with mock.patch.object(connection.urllib.request, "build_opener", return_value=opener):
                connection.forward_connection(handler, b"")
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(handler.status, 404)
            self.assertIn(b"upstream", handler.wfile.getvalue())

    def test_benchmark_results_and_cancel_are_safe_when_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = root / "profiles"
            profiles.mkdir()
            (profiles / "one.env").write_text("REQUEST_MODEL=one\nPORT=19108\n", encoding="utf-8")
            service = agent.AgentService(agent.AgentConfiguration(root=root, profiles_dir=profiles))
            self.assertEqual(service.benchmark_results()["benchmarks"], [])
            self.assertFalse(service.cancel_benchmark()["running"])

    def test_usage_store_retention_setting_has_explicit_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.UsageStore(Path(tmp) / "usage.sqlite3")
            store.set_setting("retention_days", "7")
            self.assertEqual(store.retention_days(), 7)
            store._db.close()


@unittest.skipUnless(os.name == "nt", "native Win32 process tree verification runs on Windows")
class NativeWindowsLifecycleTests(unittest.TestCase):
    def test_job_object_kills_unicode_descendant_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "child pid.json"
            script = root / "parent.py"
            script.write_text(
                "import json,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                "open(sys.argv[1],'w',encoding='utf-8').write(json.dumps({'pid':child.pid,'arg':sys.argv[2]}))\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            adapter = ps.WindowsProcessAdapter()
            with (root / "tree.log").open("wb") as log:
                proc = adapter.launch([sys.executable, str(script), str(marker), "space 模型 & quote\""], cwd=root, env=dict(os.environ), log_handle=log)
                try:
                    for _ in range(200):
                        if marker.exists():
                            break
                        __import__("time").sleep(0.05)
                    self.assertTrue(marker.exists())
                    child_pid = json.loads(marker.read_text(encoding="utf-8"))["pid"]
                    self.assertTrue(ps.process_is_alive(child_pid))
                    self.assertTrue(adapter.stop(proc.pid, force=True, timeout=5))
                    self.assertFalse(ps.process_is_alive(child_pid))
                finally:
                    if proc.poll() is None:
                        adapter.stop(proc.pid, force=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
