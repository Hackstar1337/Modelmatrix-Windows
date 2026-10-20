"""Cross-platform regression tests for the Windows support layer.

These tests use mocks on Linux but exercise the real argument/ownership code;
the same tests run natively on the Windows test host.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import platform_support as ps  # noqa: E402
import modelmatrix_agent as agent  # noqa: E402


class WindowsCommandContractTests(unittest.TestCase):
    def test_start_command_json_contains_executable(self) -> None:
        argv, shell = ps.command_argv(
            {"START_COMMAND_JSON": json.dumps([r"C:\Program Files\mm\server.exe", "--name", "a b"])},
            "ignored",
        )
        self.assertEqual(argv, [r"C:\Program Files\mm\server.exe", "--name", "a b"])
        self.assertIsNone(shell)

    def test_server_args_json_allows_empty_args_but_needs_server_bin(self) -> None:
        self.assertEqual(
            ps.command_argv({"SERVER_BIN": "server.exe", "SERVER_ARGS_JSON": "[]"}, "ignored")[0],
            ["server.exe"],
        )
        with self.assertRaises(ValueError):
            ps.command_argv({"SERVER_ARGS_JSON": "[]"}, "ignored")

    def test_server_bin_keeps_generated_adapter_flags(self) -> None:
        argv, _ = ps.command_argv(
            {"SERVER_BIN": r"C:\mm\server.exe"},
            r"llama-server -m 'C:\models\space model.gguf' --port 8123",
        )
        self.assertEqual(argv[0], r"C:\mm\server.exe")
        self.assertEqual(argv[2], r"C:\models\space model.gguf")
        self.assertIn("--port", argv)
        self.assertIn("8123", argv)

    def test_path_key_is_case_insensitive(self) -> None:
        result = ps.augment_windows_path({"Path": r"C:\Windows"}, venv=r"C:\venv")
        self.assertNotIn("PATH", result)
        self.assertTrue(result["Path"].startswith(r"C:\venv\Scripts"))

    def test_shell_command_is_not_list2cmdline_quoted(self) -> None:
        fake = mock.Mock(pid=1234)
        with mock.patch.object(ps, "IS_WINDOWS", True), \
             mock.patch.object(ps.subprocess, "Popen", return_value=fake) as popen, \
             mock.patch.object(ps.WindowsProcessAdapter, "_assign_job"), \
             mock.patch.object(ps.WindowsProcessAdapter, "_resume_process"), \
             mock.patch.object(ps, "process_identity", return_value=None):
            ps.WindowsProcessAdapter().launch(
                ["Write-Output 'a b'"], cwd=Path("."), env={}, log_handle=None, shell="powershell"
            )
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-1], "Write-Output 'a b'")
        self.assertNotIn('"Write-Output', argv[-1])


    def test_json_command_precedes_runtime_model_requirements(self) -> None:
        profile = agent.Profile("structured", {"REQUEST_MODEL": "structured", "RUNTIME": "llama.cpp", "PORT": "18181", "START_COMMAND_JSON": json.dumps([sys.executable, "-c", "print(123)"])})
        self.assertIn("print(123)", agent.build_start_command(profile))


class WindowsOwnershipTests(unittest.TestCase):
    def test_reused_pid_is_not_taskkilled(self) -> None:
        expected = ps.ProcessIdentity(42, created_ns=100, executable="server.exe")
        current = ps.ProcessIdentity(42, created_ns=200, executable="other.exe")
        adapter = ps.WindowsProcessAdapter()
        with mock.patch.object(ps, "IS_WINDOWS", True), \
             mock.patch.object(ps, "process_identity", return_value=current), \
             mock.patch.object(ps.subprocess, "run") as run:
            self.assertFalse(adapter.stop(42, expected=expected, timeout=0.01))
        run.assert_not_called()

    def test_missing_current_creation_time_is_not_ownership(self) -> None:
        self.assertFalse(ps.ProcessIdentity(42, 100, "server.exe").matches(ps.ProcessIdentity(42, None, "server.exe")))

    def test_windows_pid_probe_never_signals(self) -> None:
        with mock.patch.object(ps, "IS_WINDOWS", True), mock.patch.object(ps.os, "kill", side_effect=AssertionError("unsafe Windows signal")):
            ps._pid_exists(987654321)

    def test_config_writer_uses_shared_lock_and_tmp_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent.save_agent_config(root, {"unicode": "模型 space"})
            self.assertTrue((root / "config.json.lock").exists())
            self.assertTrue((root / "config.json").exists())
            self.assertEqual(agent.load_agent_config(root)["unicode"], "模型 space")


class WindowsListenerTests(unittest.TestCase):
    def test_listener_details_are_bulk_and_do_not_use_powershell(self) -> None:
        netstat = "  TCP    127.0.0.1:8123    0.0.0.0:0    LISTENING    100\n"
        completed = mock.Mock(stdout=netstat)
        details = {100: (r"C:\mm\server.exe", 123)}
        with mock.patch.object(ps, "IS_WINDOWS", True), \
             mock.patch.object(ps.subprocess, "run", return_value=completed) as run, \
             mock.patch.object(ps, "_windows_process_details_many", return_value=details) as bulk:
            rows = ps.WindowsListenerProvider().list_listening_tcp()
        self.assertEqual(rows[0]["command"], r"C:\mm\server.exe")
        bulk.assert_called_once_with([100])
        self.assertNotIn("powershell.exe", run.call_args.args[0])


@unittest.skipUnless(os.name == "nt", "real Win32 Job Objects require Windows")
class NativeWindowsProcessTests(unittest.TestCase):
    def test_real_unicode_argv_and_descendant_tree_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_file = root / "child identity.json"
            script = root / "tree fixture.py"
            script.write_text("import sys,subprocess,json,time\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\nopen(sys.argv[1],'w',encoding='utf-8').write(json.dumps({'pid':p.pid,'arg':sys.argv[2]}))\ntime.sleep(60)\n", encoding="utf-8")
            adapter = ps.WindowsProcessAdapter()
            with (root / "tree.log").open("wb") as log:
                proc = adapter.launch([sys.executable, str(script), str(child_file), "space 模型 & quote\""], cwd=root, env=dict(os.environ), log_handle=log)
                try:
                    deadline = time.monotonic() + 10
                    while not child_file.exists() and time.monotonic() < deadline:
                        time.sleep(.05)
                    payload = json.loads(child_file.read_text(encoding="utf-8"))
                    self.assertEqual(payload["arg"], "space 模型 & quote\"")
                    self.assertTrue(ps.process_is_alive(proc.pid))
                    self.assertTrue(ps.process_is_alive(payload["pid"]))
                    self.assertTrue(adapter.stop(proc.pid, timeout=5))
                    proc.wait(timeout=5)
                    self.assertFalse(ps.process_is_alive(payload["pid"]))
                finally:
                    if proc.poll() is None:
                        adapter.stop(proc.pid, force=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
